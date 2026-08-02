"""Execution side of the Google Sheets tool: read, append, update one row.

The LLM schema for this tool is built in
``api/services/workflow/tools/custom_tool.py`` from the column inference cached
when the sheet was linked — one named parameter per column. This module is the
other half: it takes what the model sent (``{parameter_name: value}``), turns it
back into real column headers, and lets the server decide where each value
lands.

Placement is never the model's decision
---------------------------------------
Values reach the spreadsheet through ``sheet_schema.validate_and_place``, which
resolves headers to column letters and rejects anything it does not recognise.
A parameter that does not correspond to a column of the bound sheet fails the
call with a message the agent can act on, rather than being dropped silently —
a silently dropped field is a lost lead nobody notices.

Every call re-reads the header row (a single-row read). That costs one small
Sheets request and buys two things: reads come back labelled with the live
headers, and writes can refuse to run against a cache that no longer matches the
sheet (``schema_is_stale``) instead of writing into the wrong columns.

Concurrency
-----------
``update_row`` takes a short Redis lease keyed by (spreadsheet, row) — the same
``SET NX`` pattern as
``api/services/messaging/whatsapp/conversation_service.py`` — so two calls
landing on the same row serialize instead of overwriting each other. Appends
need no lease: Google inserts them after the last row of the table server-side.

Everything an agent could hit at runtime comes back as
``{"status": "error", "error": <plain sentence>}``. Stack traces go to the log,
never to the caller: this text can end up spoken on a live call.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from uuid import uuid4

import redis.asyncio as aioredis
from loguru import logger

from api.constants import REDIS_URL
from api.services.workflow.tools.custom_tool import (
    SHEETS_MATCH_COLUMN_PARAM,
    SHEETS_MATCH_VALUE_PARAM,
    SHEETS_MAX_ROWS_PARAM,
    SHEETS_OPERATION_PARAM,
    SHEETS_RESERVED_PARAMS,
    SHEETS_VALUES_PARAM,
    _resolve_preset_parameters,
    sheets_allowed_operations,
    sheets_cached_schema,
    sheets_column_parameter_map,
)

from .oauth import GoogleOAuthError
from .sheet_schema import SheetSchema, schema_is_stale, validate_and_place
from .sheets_client import (
    GoogleSheetsClient,
    GoogleSheetsError,
    build_a1_range,
    column_index,
    column_letter,
)

DEFAULT_HEADER_ROW = 1
DEFAULT_MAX_ROWS = 25
# A voice agent cannot use hundreds of rows, and every row read is context the
# LLM has to pay for.
MAX_ROWS_CEILING = 200

# Long enough to cover a find + write round-trip to Google, short enough that a
# worker dying mid-write does not block the row for the rest of the call.
ROW_LEASE_TTL_SECONDS = 15
ROW_LEASE_ATTEMPTS = 6
ROW_LEASE_RETRY_DELAY_SECONDS = 0.25


class SheetsToolError(Exception):
    """A tool call failed for a reason the agent should hear about."""


@dataclass(frozen=True)
class _SheetBinding:
    """The organization-scoped sheet a tool is bound to."""

    organization_id: int
    spreadsheet_id: str
    sheet_name: str
    header_row: int


# ---------------------------------------------------------------------------
# Row lease (SET NX), mirroring the WhatsApp conversation lease
# ---------------------------------------------------------------------------

_redis_client: Optional[aioredis.Redis] = None


def row_lease_key(spreadsheet_id: str, row_number: int) -> str:
    """Redis key serializing writes to one row of one spreadsheet."""
    return f"sheets:row-lease:{spreadsheet_id}:{row_number}"


async def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


@asynccontextmanager
async def _row_lease(spreadsheet_id: str, row_number: int) -> AsyncIterator[None]:
    """Hold a short lease on one row for the duration of the block.

    Waits briefly for a busy row — a concurrent write is finishing, not
    stalling — then gives up with a message the agent can relay. If Redis
    itself is unreachable the write still goes through: losing the ability to
    update a row entirely is worse than the narrow race the lease closes, and
    the failure is logged loudly.
    """
    key = row_lease_key(spreadsheet_id, row_number)
    token = uuid4().hex
    acquired = False

    try:
        redis_client = await _get_redis()
        for attempt in range(1, ROW_LEASE_ATTEMPTS + 1):
            acquired = bool(
                await redis_client.set(key, token, nx=True, ex=ROW_LEASE_TTL_SECONDS)
            )
            if acquired or attempt == ROW_LEASE_ATTEMPTS:
                break
            await asyncio.sleep(ROW_LEASE_RETRY_DELAY_SECONDS)
    except Exception as e:
        logger.error(
            f"Redis is unavailable for the Sheets row lease {key}; writing "
            f"without serialization: {e}"
        )
        yield
        return

    if not acquired:
        raise SheetsToolError(
            "That row is being updated right now. Try again in a moment."
        )

    try:
        yield
    finally:
        # Best-effort release; the TTL is the safety net.
        try:
            redis_client = await _get_redis()
            if await redis_client.get(key) == token:
                await redis_client.delete(key)
        except Exception as e:
            logger.warning(f"Failed to release the Sheets row lease {key}: {e}")


# ---------------------------------------------------------------------------
# Argument resolution
# ---------------------------------------------------------------------------


def _match_key(value: Any) -> str:
    """Loose key used to recognise a column the model named differently.

    ``"First name"``, ``"first_name"`` and ``"Prénom"`` vs ``"prenom"`` all
    collapse to the same key, so a model that echoes the header text instead of
    the generated parameter name is still understood. This only ever *finds* a
    column that already exists — it never invents one.
    """
    folded = unicodedata.normalize("NFKD", str(value or ""))
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", folded.lower())


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _coerce_row_limit(value: Any, fallback: int) -> int:
    try:
        limit = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(1, min(limit, MAX_ROWS_CEILING))


def _resolve_binding(
    tool: Any, config: Dict[str, Any], organization_id: Optional[int]
) -> _SheetBinding:
    """Validate that this tool points at a sheet this organization may touch."""
    if not organization_id:
        logger.error(
            f"Sheets tool '{getattr(tool, 'name', '?')}' invoked without an "
            f"organization_id; refusing to call Google"
        )
        raise SheetsToolError("The spreadsheet connection is not available right now.")

    tool_organization_id = getattr(tool, "organization_id", None)
    if tool_organization_id is not None and int(tool_organization_id) != int(
        organization_id
    ):
        # The tool was already loaded org-scoped; this is the belt-and-braces
        # check that a mismatch can never reach Google.
        logger.error(
            f"Sheets tool {getattr(tool, 'tool_uuid', '?')} belongs to organization "
            f"{tool_organization_id} but was invoked for {organization_id}"
        )
        raise SheetsToolError("This spreadsheet is not available for this account.")

    spreadsheet_id = str(config.get("spreadsheet_id") or "").strip()
    if not spreadsheet_id:
        raise SheetsToolError("No spreadsheet is linked to this tool yet.")

    cached = sheets_cached_schema(config)
    sheet_name = str(
        config.get("sheet_name") or cached.get("sheet_name") or cached.get("tab") or ""
    ).strip()
    if not sheet_name:
        raise SheetsToolError("No sheet tab is selected for this spreadsheet.")

    try:
        header_row = max(1, int(config.get("header_row", DEFAULT_HEADER_ROW)))
    except (TypeError, ValueError):
        header_row = DEFAULT_HEADER_ROW

    return _SheetBinding(
        organization_id=int(organization_id),
        spreadsheet_id=spreadsheet_id,
        sheet_name=sheet_name,
        header_row=header_row,
    )


def _resolve_operation(config: Dict[str, Any], arguments: Dict[str, Any]) -> str:
    """Pick the operation, refusing anything this tool is not allowed to do."""
    allowed = sheets_allowed_operations(config)
    requested = str(arguments.get(SHEETS_OPERATION_PARAM) or "").strip()
    if not requested:
        if len(allowed) == 1:
            return allowed[0]
        raise SheetsToolError(f"Say which operation to run: {', '.join(allowed)}.")
    if requested not in allowed:
        raise SheetsToolError(
            f"'{requested}' is not something this sheet allows. "
            f"Available operations: {', '.join(allowed)}."
        )
    return requested


def _load_cached_schema(config: Dict[str, Any]) -> Optional[SheetSchema]:
    """Rebuild the cached column inference, or None when there is none usable.

    The cache round-trips through the database as JSON, so it comes back as a
    dict and has to be rehydrated. A cache this module cannot rebuild is treated
    as absent: the caller then places values against the sheet's live headers,
    which is slower to reason about but still correct — better than failing a
    call in progress.
    """
    raw = config.get("sheet_schema")
    if isinstance(raw, SheetSchema):
        return raw

    cached = sheets_cached_schema(config)
    if not cached or not cached.get("columns"):
        return None

    try:
        from_dict = getattr(SheetSchema, "from_dict", None)
        return from_dict(cached) if callable(from_dict) else SheetSchema(**cached)
    except Exception as e:
        logger.warning(
            f"Could not rebuild the cached sheet schema ({e}); falling back to "
            f"the sheet's live header row"
        )
        return None


def _column_resolver(config: Dict[str, Any], headers: List[str]):
    """Build ``parameter name -> real column header`` lookup.

    Rebuilt from the same cache the LLM schema was generated from, with the live
    headers as a second chance for a model that echoed the header text.
    """
    parameter_map = sheets_column_parameter_map(config)
    by_loose_key = {_match_key(header): header for header in headers if header}
    for parameter_name, header in parameter_map.items():
        by_loose_key.setdefault(_match_key(parameter_name), header)

    def resolve(key: str) -> Optional[str]:
        if key in parameter_map:
            return parameter_map[key]
        return by_loose_key.get(_match_key(key))

    return resolve, parameter_map


def _collect_values(
    config: Dict[str, Any],
    arguments: Dict[str, Any],
    preset_arguments: Dict[str, Any],
    headers: List[str],
) -> Dict[str, Any]:
    """Turn the model's named parameters into ``{column header: value}``.

    Unknown keys are refused, not ignored: a value the agent believed it saved
    must never disappear into a column that does not exist. Empty values are
    dropped — the model routinely emits nulls for columns it has nothing for,
    and writing those would blank real cells on an update.
    """
    resolve, parameter_map = _column_resolver(config, headers)

    sources: List[Dict[str, Any]] = []
    free_form = arguments.get(SHEETS_VALUES_PARAM)
    if isinstance(free_form, dict):
        # Degraded mode (no cached inference): the model was asked for a map
        # keyed by real headers.
        sources.append(free_form)
    sources.append(
        {
            key: value
            for key, value in arguments.items()
            if key not in SHEETS_RESERVED_PARAMS
        }
    )
    # Operator-configured presets win over the model, exactly as in
    # execute_http_tool.
    sources.append(preset_arguments)

    values: Dict[str, Any] = {}
    unknown: List[str] = []
    for source in sources:
        for key, value in source.items():
            header = resolve(key)
            if header is None:
                unknown.append(str(key))
                continue
            if _is_empty(value):
                continue
            values[header] = value

    if unknown:
        known = list(parameter_map) or [header for header in headers if header]
        raise SheetsToolError(
            f"This sheet has no column for: {', '.join(sorted(set(unknown)))}. "
            f"Columns you can fill in: {', '.join(known)}."
        )
    return values


def _letters_by_header(headers: List[str], letters: List[str]) -> Dict[str, str]:
    """Live header row as ``{loose key: column letter}``."""
    return {
        _match_key(header): letter
        for header, letter in zip(headers, letters)
        if header
    }


def _place(
    schema: Optional[SheetSchema],
    headers: List[str],
    letters: List[str],
    values_by_name: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve ``{header: value}`` to ``{column letter: value}``."""
    if schema is not None:
        try:
            return validate_and_place(schema, values_by_name)
        except Exception as e:
            # Placement is the last line of defence against a value landing in
            # the wrong column: when it objects, the call fails.
            logger.error(f"Sheets placement rejected {list(values_by_name)}: {e}")
            raise SheetsToolError(
                str(e) or "Those values do not fit this spreadsheet."
            ) from e

    # No usable cache: place against the header row read from the sheet itself.
    by_loose_key = _letters_by_header(headers, letters)
    placed: Dict[str, Any] = {}
    unknown: List[str] = []
    for name, value in values_by_name.items():
        letter = by_loose_key.get(_match_key(name))
        if letter is None:
            unknown.append(name)
            continue
        placed[letter] = value
    if unknown:
        raise SheetsToolError(
            f"This sheet has no column named: {', '.join(sorted(set(unknown)))}."
        )
    return placed


def _guard_against_stale_schema(
    schema: Optional[SheetSchema], headers: List[str], binding: _SheetBinding
) -> None:
    """Refuse to write when the sheet no longer matches the cached inference."""
    if schema is None or not schema_is_stale(schema, headers):
        return
    logger.error(
        f"Sheet {binding.spreadsheet_id} tab '{binding.sheet_name}' no longer "
        f"matches its cached column schema; refusing to write"
    )
    raise SheetsToolError(
        "The columns of this spreadsheet have changed since it was set up, so I "
        "cannot write to it safely. It needs to be re-linked."
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _resolve_match(
    config: Dict[str, Any],
    arguments: Dict[str, Any],
    headers: List[str],
    *,
    required: bool,
) -> Optional[Tuple[str, str]]:
    """Return ``(column header, value)`` identifying a row, if one was given."""
    resolve, parameter_map = _column_resolver(config, headers)
    raw_column = str(arguments.get(SHEETS_MATCH_COLUMN_PARAM) or "").strip()
    raw_value = arguments.get(SHEETS_MATCH_VALUE_PARAM)

    if not raw_column or _is_empty(raw_value):
        if required:
            known = list(parameter_map) or [header for header in headers if header]
            raise SheetsToolError(
                "Say which column identifies the row and what value to look for. "
                f"Columns available: {', '.join(known)}."
            )
        return None

    header = resolve(raw_column)
    if header is None:
        known = list(parameter_map) or [header for header in headers if header]
        raise SheetsToolError(
            f"This sheet has no column called '{raw_column}'. "
            f"Columns available: {', '.join(known)}."
        )
    return header, str(raw_value).strip()


async def _read_rows(
    client: GoogleSheetsClient,
    binding: _SheetBinding,
    config: Dict[str, Any],
    arguments: Dict[str, Any],
    headers: List[str],
) -> Dict[str, Any]:
    """Return rows as ``{header: value}`` maps, never as bare arrays."""
    match = _resolve_match(config, arguments, headers, required=False)
    max_rows = _coerce_row_limit(
        arguments.get(SHEETS_MAX_ROWS_PARAM),
        _coerce_row_limit(config.get("read_max_rows"), DEFAULT_MAX_ROWS),
    )

    first_data_row = binding.header_row + 1
    last_column = column_letter(len(headers) - 1)
    if match is None:
        # Unfiltered: bound the request itself so a 50k-row sheet does not
        # travel over the wire to be thrown away here.
        cells = f"A{first_data_row}:{last_column}{first_data_row + max_rows - 1}"
    else:
        cells = f"A{first_data_row}:{last_column}"

    raw_rows = await client.read_range(
        binding.spreadsheet_id, build_a1_range(binding.sheet_name, cells)
    )

    needle = _match_key(match[1]) if match else None
    rows: List[Dict[str, Any]] = []
    for offset, raw_row in enumerate(raw_rows):
        if not any(str(cell).strip() for cell in raw_row):
            continue
        padded = list(raw_row) + [""] * (len(headers) - len(raw_row))
        values = {
            header: value for header, value in zip(headers, padded) if header
        }
        if match is not None and _match_key(values.get(match[0], "")) != needle:
            continue
        rows.append({"row_number": first_data_row + offset, "values": values})
        if len(rows) >= max_rows:
            break

    return {
        "status": "success",
        "operation": "read_rows",
        "sheet_name": binding.sheet_name,
        "columns": [header for header in headers if header],
        "row_count": len(rows),
        "rows": rows,
    }


async def _append_row(
    client: GoogleSheetsClient,
    binding: _SheetBinding,
    schema: Optional[SheetSchema],
    values_by_name: Dict[str, Any],
    headers: List[str],
    letters: List[str],
) -> Dict[str, Any]:
    """Add one row at the bottom of the tab.

    No lease: Google appends after the last row of the table server-side, so
    concurrent appends get distinct rows by construction.
    """
    if not values_by_name:
        raise SheetsToolError(
            "There is nothing to write — no column values were given."
        )

    placed = _place(schema, headers, letters, values_by_name)
    if not placed:
        raise SheetsToolError("None of those values map to a column of this sheet.")

    width = max(len(headers), max(column_index(letter) for letter in placed) + 1)
    ordered: List[Any] = [""] * width
    for letter, value in placed.items():
        ordered[column_index(letter)] = value

    result = await client.append_row(
        binding.spreadsheet_id, binding.sheet_name, ordered
    )
    return {
        "status": "success",
        "operation": "append_row",
        "sheet_name": binding.sheet_name,
        "row_number": result.get("row_number"),
        "written": values_by_name,
    }


async def _update_row(
    client: GoogleSheetsClient,
    binding: _SheetBinding,
    config: Dict[str, Any],
    arguments: Dict[str, Any],
    schema: Optional[SheetSchema],
    values_by_name: Dict[str, Any],
    headers: List[str],
    letters: List[str],
) -> Dict[str, Any]:
    """Change values on the first row whose key column matches."""
    match_header, match_value = _resolve_match(
        config, arguments, headers, required=True
    )
    # The column searched is resolved from the sheet's own header row — the
    # model names a column, it never names a cell address.
    match_letter = _letters_by_header(headers, letters).get(_match_key(match_header))
    if match_letter is None:
        raise SheetsToolError(f"This sheet has no column named '{match_header}'.")

    # Never overwrite the value the row was found by.
    values_by_name = {
        name: value for name, value in values_by_name.items() if name != match_header
    }
    if not values_by_name:
        raise SheetsToolError(
            "There is nothing to update — no new values were given."
        )

    row_number = await client.find_row_by_value(
        binding.spreadsheet_id,
        binding.sheet_name,
        match_letter,
        match_value,
        start_row=binding.header_row + 1,
    )
    if row_number is None:
        raise SheetsToolError(
            f"I could not find a row where {match_header} is '{match_value}'."
        )

    placed = _place(schema, headers, letters, values_by_name)
    async with _row_lease(binding.spreadsheet_id, row_number):
        await client.update_cells(
            binding.spreadsheet_id, binding.sheet_name, row_number, placed
        )

    return {
        "status": "success",
        "operation": "update_row",
        "sheet_name": binding.sheet_name,
        "row_number": row_number,
        "matched_on": {match_header: match_value},
        "written": values_by_name,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def execute_sheets_tool(
    tool: Any,
    arguments: Dict[str, Any],
    call_context_vars: Optional[Dict[str, Any]] = None,
    gathered_context_vars: Optional[Dict[str, Any]] = None,
    organization_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute a Google Sheets tool call.

    Args:
        tool: ToolModel instance whose ``definition.type`` is ``google_sheets``
        arguments: Arguments passed by the LLM (parameter name -> value)
        call_context_vars: Initial context variables available at runtime
        gathered_context_vars: Variables extracted during the conversation
        organization_id: Organization the call belongs to; the sheet is reached
            through this organization's Google connection

    Returns:
        Result dict with the rows read / the row written, or ``{"status":
        "error", "error": ...}`` carrying a sentence the agent can say.
    """
    definition = tool.definition or {}
    config = definition.get("config", {}) or {}
    arguments = dict(arguments or {})

    try:
        binding = _resolve_binding(tool, config, organization_id)
        operation = _resolve_operation(config, arguments)

        logger.info(
            f"Executing sheets tool '{tool.name}' ({tool.tool_uuid}): {operation} "
            f"on {binding.spreadsheet_id} tab '{binding.sheet_name}'"
        )

        preset_arguments = _resolve_preset_parameters(
            config, call_context_vars, gathered_context_vars
        )

        client = await GoogleSheetsClient.for_organization(binding.organization_id)
        headers, letters = await client.get_header_row(
            binding.spreadsheet_id,
            binding.sheet_name,
            header_row=binding.header_row,
        )
        schema = _load_cached_schema(config)

        if operation == "read_rows":
            return await _read_rows(client, binding, config, arguments, headers)

        _guard_against_stale_schema(schema, headers, binding)
        values_by_name = _collect_values(config, arguments, preset_arguments, headers)

        if operation == "append_row":
            return await _append_row(
                client, binding, schema, values_by_name, headers, letters
            )
        return await _update_row(
            client,
            binding,
            config,
            arguments,
            schema,
            values_by_name,
            headers,
            letters,
        )

    except SheetsToolError as e:
        logger.info(f"Sheets tool '{tool.name}' refused the call: {e}")
        return {"status": "error", "error": str(e)}
    except GoogleOAuthError as e:
        logger.error(f"Sheets tool '{tool.name}' has no usable Google connection: {e}")
        return {
            "status": "error",
            "error": "The Google account for this spreadsheet needs to be reconnected.",
        }
    except GoogleSheetsError as e:
        logger.error(f"Sheets tool '{tool.name}' failed against Google: {e}")
        if e.requires_reconsent:
            message = (
                "The Google account for this spreadsheet needs to be reconnected."
            )
        elif e.http_status == 404:
            message = "That spreadsheet is no longer reachable."
        elif e.retryable:
            message = "The spreadsheet is temporarily unavailable. Try again shortly."
        else:
            message = "The spreadsheet could not be updated."
        return {"status": "error", "error": message}
    except ValueError as e:
        # Bad column letter, malformed preset parameter, ... — a configuration
        # or mapping problem, not something the agent did wrong.
        logger.error(f"Sheets tool '{tool.name}' rejected its arguments: {e}")
        return {"status": "error", "error": "Those values do not fit this spreadsheet."}
    except Exception as e:
        logger.exception(f"Sheets tool '{tool.name}' execution failed: {e}")
        return {
            "status": "error",
            "error": "The spreadsheet could not be reached right now.",
        }
