"""Understand an arbitrary Google Sheet without configuring a single column.

The owner points at a tab; this module works out what every column *means* and
turns that into named function parameters the agent's LLM can fill.

Why the inference runs once
---------------------------
:func:`infer_sheet_schema` costs one LLM round-trip, so it runs at bind time and
the result is cached on the tool definition together with
:func:`headers_fingerprint` of the header row. :func:`schema_is_stale` says when
to redo it — only when the headers actually changed. Inferring on every tool call
would add a round-trip to each conversation turn *and* would let the same column
be interpreted differently from one call to the next.

Why named parameters, not a "values" array
------------------------------------------
:func:`schema_to_tool_parameters` emits one parameter per column, named after the
real header and described in French by the inference. A model places a value far
more reliably into ``date_de_rappel`` ("date du prochain rappel convenu avec le
client") than into the 7th slot of an anonymous array. The output plugs straight
into ``config.parameters`` of a ToolModel definition, so
``services/workflow/tools/custom_tool.py::tool_to_function_schema`` stays the
single passage point — exactly like ``config.resolver`` does for transfer_call.

Why the server does the placement
---------------------------------
The model returns ``{column_name: value}``; :func:`validate_and_place` resolves
each key against the *real* headers and returns ``{column_letter: value}`` for
``GoogleSheetsClient.update_cells`` (or :func:`ordered_row_values` for
``append_row``). The model never sees or picks a column letter. An unknown key
raises :class:`UnknownSheetColumnError` naming the valid columns instead of being
dropped: a value silently lost in a CRM is worse than a loud failure.

Degraded mode is still a working sheet
--------------------------------------
If the LLM is unreachable or answers unparseable JSON, inference falls back to a
deterministic schema — every header becomes a ``other`` column described by its
own header text. The sheet stays usable; only the phrasing shown to the agent is
poorer.

Multi-tenancy
-------------
A sheet belongs to an organization. Inference runs on *that* organization's
configured LLM, resolved through
``services/configuration/ai_model_configuration.py``. No new provider, no
hard-coded key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from api.services.gen_ai.json_parser import parse_llm_json

from .sheets_client import column_index, column_letter, normalize_column_letter

SHEET_SCHEMA_VERSION = 1

# A soft catalogue: it steers the inference towards vocabulary the rest of the
# product understands, but a column the model cannot place is legitimately
# "other" and must not be forced into a role it does not have. Roles outside this
# tuple are kept when they look like clean slugs (see _normalize_role).
SHEET_COLUMN_ROLES = (
    "contact_name",
    "phone",
    "email",
    "status",
    "datetime",
    "amount",
    "address",
    "notes",
    "consent",
    "other",
)
_DEFAULT_ROLE = "other"

# Prompt budget. Sample values disambiguate ("Date" = callback date or created
# date?) but a whole sheet in the prompt is expensive and leaks more personal
# data than the inference needs.
_MAX_SAMPLE_ROWS = 5
_MAX_SAMPLES_PER_COLUMN = 4
_MAX_CELL_CHARS = 80

# Bind time is a user-facing request; a hung provider must not hold it open.
# Past this we take the deterministic fallback.
_INFERENCE_TIMEOUT_SECONDS = 45.0

_MAX_DESCRIPTION_CHARS = 240
_MAX_VALUE_FORMAT_CHARS = 40
_MAX_ROLE_CHARS = 32
_MAX_PARAMETER_NAME_CHARS = 48

# Number of column names listed back when a key is rejected. Long enough to be
# actionable, short enough not to flood a tool-call error into the transcript.
_MAX_LISTED_COLUMNS = 30

# GoogleSheetsClient writes with valueInputOption=USER_ENTERED, i.e. values are
# parsed the way a human typing into the cell would be. "+212600112233" would be
# evaluated as arithmetic and stored as the number 212600112233, and
# "0612345678" would lose its leading zero. A leading apostrophe is the Sheets
# escape that forces the cell to stay text; it is not displayed in the cell.
_TEXT_LITERAL_PREFIX = "'"

_INFERENCE_SYSTEM_PROMPT = """\
You map the columns of a spreadsheet used by a voice AI agent.

You receive the header row and a few real rows. Use the sample values to remove
ambiguity: a column headed "Date" may be a callback date or a creation date, and
only the values tell you which.

Answer with a single JSON object, no markdown fence, no commentary:

{"columns": [{"index": 0, "header": "<the header, copied exactly>",
              "role": "<role>", "description": "<one sentence, in French>",
              "value_format": "<observed format, or empty>"}]}

Rules:
- Return exactly one entry per column you were given, in the same order, and
  copy each header verbatim so it can be matched back.
- "role" is one of: contact_name, phone, email, status, datetime, amount,
  address, notes, consent, other. Use "other" whenever nothing fits — it is a
  valid answer and a wrong role is worse than "other".
- "description" MUST be written in French and MUST be actionable: it is shown to
  the agent as the description of a function parameter, so it tells the agent
  WHAT TO PUT in the column, not what the column is.
  Write "Date du prochain rappel convenu avec le client, au format AAAA-MM-JJ.",
  not "Colonne contenant une date."
- "value_format" is the shape observed in the samples, e.g. "+212XXXXXXXXX",
  "oui/non", "YYYY-MM-DD", "0,00 MAD". Leave it empty if the samples show no
  consistent shape. Do not invent a format the samples do not support.
"""


class SheetSchemaError(ValueError):
    """A sheet schema could not be built, or a value could not be placed."""


class UnknownSheetColumnError(SheetSchemaError):
    """A key handed in for placement matches no column of the sheet."""


class AmbiguousSheetColumnError(SheetSchemaError):
    """A key for placement matches several columns, or two keys the same one."""


# ---------------------------------------------------------------------------
# Schema model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SheetColumn:
    """One column of a bound sheet, as the agent will see it.

    ``index``/``letter`` are always computed by the server from the column's real
    position in the header row — never taken from the LLM answer.
    """

    index: int
    letter: str
    header: str
    name: str
    role: str = _DEFAULT_ROLE
    description: str = ""
    value_format: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "letter": self.letter,
            "header": self.header,
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "value_format": self.value_format,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SheetColumn | None:
        """Rebuild a column from stored JSON, or None when it is unusable.

        Tolerant on purpose: the schema lives inside ``ToolModel.definition`` and
        may have been written by an older version. A column that cannot be
        anchored to a position is dropped rather than being given a guessed one,
        because a wrong position writes into the wrong column.
        """
        index = _as_int(data.get("index"))
        letter = str(data.get("letter") or "").strip()
        if index is None and letter:
            try:
                index = column_index(letter)
            except ValueError:
                return None
        if index is None or index < 0:
            return None

        header = str(data.get("header") or "").strip()
        name = _slugify_parameter_name(data.get("name") or "") or _slugify_parameter_name(
            header
        )
        if not name:
            name = f"column_{column_letter(index).lower()}"
        return cls(
            index=index,
            letter=column_letter(index),
            header=header,
            name=name,
            role=_normalize_role(data.get("role")),
            description=_clean_text(data.get("description"), _MAX_DESCRIPTION_CHARS),
            value_format=_clean_text(data.get("value_format"), _MAX_VALUE_FORMAT_CHARS),
        )


@dataclass(frozen=True)
class SheetSchema:
    """What a bound sheet means, cached against a fingerprint of its headers.

    JSON-serializable through :meth:`to_dict` so it can be stored as-is in
    ``ToolModel.definition`` and read back with :meth:`from_dict`.
    """

    fingerprint: str
    headers: tuple[str, ...] = ()
    columns: tuple[SheetColumn, ...] = ()
    sheet_name: str | None = None
    header_row: int = 1
    source: str = "fallback"  # "llm" when the inference succeeded
    model: str | None = None
    inferred_at: str = ""
    version: int = SHEET_SCHEMA_VERSION
    # Set when the LLM was tried and did not answer usably. Surfaced to the UI so
    # the owner knows the descriptions are the degraded ones.
    degraded_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "fingerprint": self.fingerprint,
            "headers": list(self.headers),
            "columns": [column.to_dict() for column in self.columns],
            "sheet_name": self.sheet_name,
            "header_row": self.header_row,
            "source": self.source,
            "model": self.model,
            "inferred_at": self.inferred_at,
            "degraded_reason": self.degraded_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> SheetSchema | None:
        """Rebuild a schema from stored JSON, or None when there is nothing usable."""
        if not isinstance(data, Mapping):
            return None
        raw_columns = data.get("columns")
        columns: list[SheetColumn] = []
        if isinstance(raw_columns, Sequence) and not isinstance(raw_columns, (str, bytes)):
            for raw_column in raw_columns:
                if not isinstance(raw_column, Mapping):
                    continue
                column = SheetColumn.from_dict(raw_column)
                if column is not None:
                    columns.append(column)
        if not columns:
            return None

        headers = data.get("headers")
        if isinstance(headers, Sequence) and not isinstance(headers, (str, bytes)):
            header_tuple = tuple(str(header) for header in headers)
        else:
            header_tuple = tuple(column.header for column in columns)

        fingerprint = str(data.get("fingerprint") or "") or headers_fingerprint(
            header_tuple
        )
        # `or` would be wrong for both of these: an explicit version 0 must stay
        # 0 so schema_is_stale still sees it as outdated, and a header_row below
        # 1 is not a row number at all.
        version = _as_int(data.get("version"))
        header_row = _as_int(data.get("header_row"))
        return cls(
            fingerprint=fingerprint,
            headers=header_tuple,
            columns=tuple(sorted(columns, key=lambda column: column.index)),
            sheet_name=(str(data["sheet_name"]) if data.get("sheet_name") else None),
            header_row=header_row if header_row and header_row >= 1 else 1,
            source=str(data.get("source") or "fallback"),
            model=(str(data["model"]) if data.get("model") else None),
            inferred_at=str(data.get("inferred_at") or ""),
            version=version if version is not None else SHEET_SCHEMA_VERSION,
            degraded_reason=(
                str(data["degraded_reason"]) if data.get("degraded_reason") else None
            ),
        )

    @property
    def is_degraded(self) -> bool:
        return self.source != "llm"

    def column_by_letter(self, letter: str) -> SheetColumn | None:
        """Reverse lookup, to report back which column a value was written to."""
        normalized = normalize_column_letter(letter)
        return next(
            (column for column in self.columns if column.letter == normalized), None
        )


# ---------------------------------------------------------------------------
# Fingerprint / staleness
# ---------------------------------------------------------------------------


def headers_fingerprint(headers: Iterable[Any]) -> str:
    """Stable fingerprint of a header row, blind to case and whitespace.

    ``["Nom du client", "Téléphone"]`` and ``["  nom du   client ", "TÉLÉPHONE"]``
    fingerprint identically: re-capitalising a header is not a schema change and
    must not cost an LLM round-trip. Trailing blank cells are ignored (Sheets
    trims them inconsistently between reads) but interior blanks are kept, since
    they shift every following column.
    """
    normalized = [_normalize_header(header) for header in headers]
    while normalized and not normalized[-1]:
        normalized.pop()
    # \x1f (unit separator) cannot appear in a Sheets header, so no pair of
    # different header rows can collide by concatenation.
    joined = "\x1f".join(normalized)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def schema_is_stale(
    cached_schema: SheetSchema | Mapping[str, Any] | None,
    current_headers: Iterable[Any],
) -> bool:
    """True when the cached schema no longer matches the sheet's headers.

    Callers re-run :func:`infer_sheet_schema` on True. A client who renames or
    inserts a column gets a fresh schema on the next use instead of having to
    reconfigure anything.
    """
    schema = (
        cached_schema
        if isinstance(cached_schema, SheetSchema)
        else SheetSchema.from_dict(cached_schema)
    )
    if schema is None or not schema.fingerprint:
        return True
    if schema.version != SHEET_SCHEMA_VERSION:
        return True
    return schema.fingerprint != headers_fingerprint(current_headers)


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


async def infer_sheet_schema(
    headers: Sequence[Any],
    sample_rows: Sequence[Sequence[Any]] | None,
    organization_id: int | None,
    *,
    sheet_name: str | None = None,
    header_row: int = 1,
) -> SheetSchema:
    """Work out what each column means, using the organization's configured LLM.

    ``headers`` is the real header row (blank cells kept in place, as
    ``GoogleSheetsClient.get_header_row`` returns them) and ``sample_rows`` a
    handful of data rows. Ragged rows are fine: Sheets drops trailing empty
    cells, and a short row simply contributes no example for the columns it does
    not reach.

    Never raises for an LLM problem. If the model is unreachable, times out, or
    answers something unparseable, the returned schema is the deterministic
    fallback (every column ``role="other"``, described by its own header) with
    ``source="fallback"`` and ``degraded_reason`` set. A usable sheet in degraded
    mode beats a binding that fails.

    Raises :class:`SheetSchemaError` only when the header row itself carries no
    usable column, because there is then nothing to describe.
    """
    cleaned_headers = tuple(str(header or "").strip() for header in headers or ())
    schema = _fallback_schema(
        cleaned_headers, sheet_name=sheet_name, header_row=header_row
    )

    samples = _collect_samples(cleaned_headers, sample_rows)
    llm = await _resolve_organization_llm(organization_id)
    if llm is None:
        logger.info(
            f"No LLM configured for organization {organization_id}; using the "
            f"deterministic sheet schema for {len(schema.columns)} column(s)"
        )
        return replace(schema, degraded_reason="llm_not_configured")

    service, model = llm
    user_prompt = _build_inference_prompt(schema, samples)
    try:
        raw_response = await asyncio.wait_for(
            _run_inference(service, user_prompt),
            timeout=_INFERENCE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Sheet column inference timed out after {_INFERENCE_TIMEOUT_SECONDS}s "
            f"for organization {organization_id}; falling back"
        )
        return replace(schema, model=model, degraded_reason="llm_timeout")
    except Exception as exc:  # noqa: BLE001 - any provider failure degrades, never blocks
        logger.warning(
            f"Sheet column inference failed for organization {organization_id}: "
            f"{exc}; falling back"
        )
        return replace(schema, model=model, degraded_reason="llm_error")

    inferred = _parse_inference_response(raw_response, schema.columns)
    if not inferred:
        logger.warning(
            f"Sheet column inference returned no usable column for organization "
            f"{organization_id}; falling back"
        )
        return replace(schema, model=model, degraded_reason="llm_invalid_response")

    columns = tuple(
        replace(
            column,
            role=inferred[column.index].get("role", column.role),
            description=inferred[column.index].get("description") or column.description,
            value_format=inferred[column.index].get("value_format", ""),
        )
        if column.index in inferred
        else column
        for column in schema.columns
    )
    logger.info(
        f"Inferred {len(inferred)}/{len(columns)} column meanings for sheet "
        f"'{sheet_name or ''}' of organization {organization_id} with {model}"
    )
    return replace(schema, columns=columns, source="llm", model=model)


def _fallback_schema(
    headers: Sequence[str],
    *,
    sheet_name: str | None,
    header_row: int,
) -> SheetSchema:
    """Deterministic schema: each header describes itself, role "other".

    Blank header cells — including whitespace-only ones — are skipped, since a
    column with no name cannot be addressed by name. The positions of the columns
    after them are preserved, because ``index`` is the real position in the row.
    """
    stripped = tuple(str(header or "").strip() for header in headers)
    columns: list[SheetColumn] = []
    used_names: set[str] = set()
    for index, header in enumerate(stripped):
        if not header:
            continue
        letter = column_letter(index)
        name = _unique_parameter_name(header, letter, used_names)
        columns.append(
            SheetColumn(
                index=index,
                letter=letter,
                header=header,
                name=name,
                role=_DEFAULT_ROLE,
                description=header,
                value_format="",
            )
        )

    if not columns:
        raise SheetSchemaError(
            "This sheet has no named column in its header row; add headers before "
            "binding it."
        )

    return SheetSchema(
        fingerprint=headers_fingerprint(stripped),
        headers=stripped,
        columns=tuple(columns),
        sheet_name=sheet_name,
        header_row=header_row,
        source="fallback",
        inferred_at=datetime.now(UTC).isoformat(),
    )


async def _resolve_organization_llm(organization_id: int | None) -> tuple[Any, str] | None:
    """Build the organization's configured LLM service, or None if there is none.

    Reuses the existing resolution chain — the org's model configuration decides
    the provider, the model and the key — so inference bills and behaves exactly
    like every other LLM call in the product. Imports are deferred: this module
    is loaded during integration discovery, and pulling in the pipecat service
    factory at import time would make node-type enumeration heavy.
    """
    if organization_id is None:
        return None

    from api.services.configuration.ai_model_configuration import (
        get_resolved_ai_model_configuration,
    )

    try:
        resolved = await get_resolved_ai_model_configuration(
            organization_id=organization_id
        )
    except Exception as exc:  # noqa: BLE001 - configuration problems degrade, never block
        logger.warning(
            f"Could not resolve the AI model configuration of organization "
            f"{organization_id}: {exc}"
        )
        return None

    llm_config = resolved.effective.model_dump(exclude_none=True).get("llm") or {}
    api_key = llm_config.get("api_key", "")
    if isinstance(api_key, list):
        api_key = random.choice(api_key) if api_key else ""
    if not api_key:
        return None

    provider = llm_config.get("provider", "openai")
    model = llm_config.get("model", "gpt-4.1")
    kwargs: dict[str, Any] = {}
    if provider == "azure":
        kwargs["endpoint"] = llm_config.get("endpoint", "")
    elif provider == "openrouter" and llm_config.get("base_url"):
        kwargs["base_url"] = llm_config["base_url"]

    from api.services.pipecat.service_factory import create_llm_service_from_provider

    try:
        service = create_llm_service_from_provider(provider, model, api_key, **kwargs)
    except Exception as exc:  # noqa: BLE001 - an unsupported provider degrades, never blocks
        logger.warning(
            f"Could not build an LLM service ({provider}/{model}) for organization "
            f"{organization_id}: {exc}"
        )
        return None
    return service, model


async def _run_inference(service: Any, user_prompt: str) -> str | None:
    """One out-of-band completion, the same way QA and variable extraction do it."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    context = LLMContext()
    context.set_messages([{"role": "user", "content": user_prompt}])
    return await service.run_inference(
        context, system_instruction=_INFERENCE_SYSTEM_PROMPT
    )


def _collect_samples(
    headers: Sequence[str],
    sample_rows: Sequence[Sequence[Any]] | None,
) -> dict[int, list[str]]:
    """Per-column example values, de-duplicated, trimmed and capped.

    Rows coming back from Sheets are ragged (trailing empty cells are dropped),
    so short rows simply contribute nothing for the columns they do not reach.
    """
    samples: dict[int, list[str]] = {}
    for row in list(sample_rows or [])[:_MAX_SAMPLE_ROWS]:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        for index in range(len(headers)):
            if index >= len(row):
                continue
            value = _clean_text(row[index], _MAX_CELL_CHARS)
            if not value:
                continue
            bucket = samples.setdefault(index, [])
            if len(bucket) < _MAX_SAMPLES_PER_COLUMN and value not in bucket:
                bucket.append(value)
    return samples


def _build_inference_prompt(schema: SheetSchema, samples: Mapping[int, list[str]]) -> str:
    """Describe the sheet as compact JSON so the model can echo positions back."""
    payload = {
        "sheet_name": schema.sheet_name or "",
        "columns": [
            {
                "index": column.index,
                "letter": column.letter,
                "header": column.header,
                "samples": samples.get(column.index, []),
            }
            for column in schema.columns
        ],
    }
    return (
        "Spreadsheet to map:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"Return the JSON object described above, with exactly "
        f"{len(schema.columns)} entries in \"columns\"."
    )


def _parse_inference_response(
    raw_response: str | None,
    columns: Sequence[SheetColumn],
) -> dict[int, dict[str, str]]:
    """Turn the model's answer into ``{column index: {role, description, format}}``.

    Entries are matched back by header first and by index second — never by the
    letter the model returned, which is exactly the kind of thing a model gets
    subtly wrong. Anything that matches no real column is discarded.
    """
    if not raw_response:
        return {}

    parsed = parse_llm_json(raw_response)
    if isinstance(parsed, Mapping):
        if "raw" in parsed and len(parsed) == 1:
            return {}
        entries = parsed.get("columns")
    elif isinstance(parsed, list):
        entries = parsed
    else:
        return {}

    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return {}

    by_header: dict[str, SheetColumn] = {}
    duplicated_headers: set[str] = set()
    for column in columns:
        key = _normalize_header(column.header)
        if key in by_header:
            duplicated_headers.add(key)
        by_header[key] = column
    by_index = {column.index: column for column in columns}

    inferred: dict[int, dict[str, str]] = {}
    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue

        column = None
        header_key = _normalize_header(entry.get("header"))
        if header_key and header_key not in duplicated_headers:
            column = by_header.get(header_key)
        if column is None:
            index = _as_int(entry.get("index"))
            if index is None and position < len(columns):
                # The model returns one entry per column in order; when it omits
                # index and header we can still trust the ordering it was asked
                # to keep.
                index = columns[position].index
            column = by_index.get(index) if index is not None else None
        if column is None or column.index in inferred:
            continue

        inferred[column.index] = {
            "role": _normalize_role(entry.get("role")),
            "description": _clean_text(
                entry.get("description"), _MAX_DESCRIPTION_CHARS
            ),
            "value_format": _clean_text(
                entry.get("value_format"), _MAX_VALUE_FORMAT_CHARS
            ),
        }
    return inferred


# ---------------------------------------------------------------------------
# Exposing the schema to the agent's LLM
# ---------------------------------------------------------------------------


def schema_to_tool_parameters(
    schema: SheetSchema,
    *,
    required_roles: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Named parameters for the agent, one per column, in ``config.parameters`` shape.

    The list is what ``tool_to_function_schema`` already consumes, so a sheet tool
    resolves its parameters dynamically from the cached schema without that
    function learning anything about sheets — the same seam ``config.resolver``
    uses for transfer_call.

    Every parameter is optional by default: forcing the agent to fill all twelve
    columns of a CRM is how you get invented values. Pass ``required_roles`` to
    demand the few that genuinely gate the row (usually ``("phone",)``).
    """
    required = {role for role in required_roles}
    parameters: list[dict[str, Any]] = []
    for column in schema.columns:
        parameters.append(
            {
                "name": column.name,
                "type": _parameter_type(column),
                "description": _parameter_description(column),
                "required": column.role in required,
            }
        )
    return parameters


def _parameter_type(column: SheetColumn) -> str:
    """JSON type for a column. Phones stay strings — "+212..." is not a number."""
    if column.role == "amount":
        return "number"
    if _boolean_tokens(column.value_format) is not None:
        return "boolean"
    return "string"


def _parameter_description(column: SheetColumn) -> str:
    """What the agent's model actually reads when deciding what to put here."""
    description = column.description or column.header
    details = [f"colonne « {column.header} »"]
    if column.value_format:
        details.append(f"format attendu : {column.value_format}")
    return f"{description} ({', '.join(details)})"


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def validate_and_place(
    schema: SheetSchema,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve ``{column name or header or role: value}`` to ``{column letter: value}``.

    Keys are resolved in this order: exact parameter name, then header, then
    role. The result is ready for ``GoogleSheetsClient.update_cells``; use
    :func:`ordered_row_values` for ``append_row``.

    A key that matches nothing raises :class:`UnknownSheetColumnError` naming the
    valid columns, and two keys landing on the same column raise
    :class:`AmbiguousSheetColumnError`. Neither is ever swallowed: a value quietly
    dropped on the floor of a CRM is worse than a visible error the agent can
    retry. ``None`` values are the one exception — they mean "nothing to write",
    not "write nothing", so they are skipped.

    Values are softly coerced from ``value_format`` (phone normalisation, date
    layout, oui/non tokens). Coercion never fails: a value it cannot interpret is
    written through unchanged.
    """
    if not isinstance(values, Mapping):
        raise SheetSchemaError(
            f"Expected a mapping of column names to values, got "
            f"{type(values).__name__}."
        )

    by_name = {column.name.casefold(): column for column in schema.columns}
    by_header: dict[str, list[SheetColumn]] = {}
    by_role: dict[str, list[SheetColumn]] = {}
    for column in schema.columns:
        by_header.setdefault(_normalize_header(column.header), []).append(column)
        by_role.setdefault(column.role, []).append(column)

    placed: dict[str, Any] = {}
    placed_by: dict[str, str] = {}
    for raw_key, value in values.items():
        key = str(raw_key or "").strip()
        if not key:
            raise UnknownSheetColumnError(
                f"Empty column name. Valid columns: {_column_list(schema)}."
            )

        column = by_name.get(key.casefold())
        if column is None:
            column = _single_match(by_header.get(_normalize_header(key)), key, schema)
        if column is None:
            # "other" is a catch-all, never an address: several columns share it
            # and picking one would be a coin flip.
            role_key = key.casefold().replace(" ", "_")
            if role_key != _DEFAULT_ROLE:
                column = _single_match(by_role.get(role_key), key, schema)
        if column is None:
            raise UnknownSheetColumnError(
                f"'{key}' is not a column of this sheet. Valid columns: "
                f"{_column_list(schema)}."
            )

        if value is None:
            logger.debug(f"Skipping column '{column.header}': no value provided")
            continue

        if column.letter in placed:
            raise AmbiguousSheetColumnError(
                f"'{key}' and '{placed_by[column.letter]}' both target the column "
                f"'{column.header}' ({column.letter}); provide it once."
            )
        placed[column.letter] = coerce_value(column, value)
        placed_by[column.letter] = key

    return placed


def ordered_row_values(schema: SheetSchema, placed: Mapping[str, Any]) -> list[Any]:
    """Positional row for ``GoogleSheetsClient.append_row``, padded to the header width.

    Takes the ``{column letter: value}`` returned by :func:`validate_and_place`;
    columns with no value become empty strings so every value lands under its own
    header instead of shifting left.
    """
    width = len(schema.headers) or (max(c.index for c in schema.columns) + 1)
    for letter in placed:
        width = max(width, column_index(letter) + 1)
    row: list[Any] = [""] * width
    for letter, value in placed.items():
        row[column_index(letter)] = value
    return row


def _single_match(
    candidates: list[SheetColumn] | None,
    key: str,
    schema: SheetSchema,
) -> SheetColumn | None:
    if not candidates:
        return None
    if len(candidates) > 1:
        raise AmbiguousSheetColumnError(
            f"'{key}' matches several columns of this sheet "
            f"({', '.join(column.letter for column in candidates)}); use the exact "
            f"column name instead. Valid columns: {_column_list(schema)}."
        )
    return candidates[0]


def _column_list(schema: SheetSchema) -> str:
    names = [column.name for column in schema.columns]
    shown = names[:_MAX_LISTED_COLUMNS]
    listed = ", ".join(shown)
    if len(names) > len(shown):
        listed += f", … (+{len(names) - len(shown)})"
    return listed


# ---------------------------------------------------------------------------
# Soft coercion
# ---------------------------------------------------------------------------


def coerce_value(column: SheetColumn, value: Any) -> Any:
    """Nudge a value towards the column's observed format. Never raises.

    Soft on purpose: the goal is that "0612345678" and "06 12 34 56 78" land in
    the sheet the same way, not that the agent's answer be rejected over
    punctuation.
    """
    try:
        if _is_phone(column):
            return _coerce_phone(value, column.value_format)

        tokens = _boolean_tokens(column.value_format)
        if tokens is not None:
            coerced = _coerce_boolean(value, tokens)
            if coerced is not None:
                return coerced

        if _is_datetime(column):
            return _coerce_datetime(value, column.value_format)

        if isinstance(value, bool) or isinstance(value, (int, float)):
            return value
        if isinstance(value, (Mapping, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()
    except Exception as exc:  # noqa: BLE001 - coercion is a nicety, the value matters more
        logger.debug(f"Leaving the value of column '{column.header}' as-is: {exc}")
        return value


def _is_phone(column: SheetColumn) -> bool:
    if column.role == "phone":
        return True
    compact = re.sub(r"[\s\-().]", "", column.value_format or "").upper()
    return bool(re.fullmatch(r"\+?\d{0,4}X{4,}", compact))


def _coerce_phone(value: Any, value_format: str) -> Any:
    """Strip separators, keep the "+", and expand a national "0" prefix.

    ``value_format`` "+212XXXXXXXXX" tells us the country code, so "0612345678"
    becomes "+212612345678". The result is escaped as a text literal — see
    ``_TEXT_LITERAL_PREFIX``, without it Sheets eats both the "+" and the leading
    zero.
    """
    raw = str(value).strip()
    compact = re.sub(r"[\s  .\-()/]", "", raw)
    if not re.fullmatch(r"\+?\d{4,}", compact):
        # Extensions, "à rappeler", anything non-numeric: leave it alone rather
        # than mangle it, and still protect it from being re-parsed.
        return _as_text_literal(raw)

    if compact.startswith("00"):
        compact = "+" + compact[2:]
    elif compact.startswith("0"):
        prefix = _country_prefix(value_format)
        if prefix:
            compact = prefix + compact[1:]
    return _as_text_literal(compact)


def _country_prefix(value_format: str) -> str:
    match = re.match(r"\s*(\+\d{1,4})", value_format or "")
    return match.group(1) if match else ""


def _as_text_literal(value: str) -> str:
    if not value or value.startswith(_TEXT_LITERAL_PREFIX):
        return value
    return f"{_TEXT_LITERAL_PREFIX}{value}"


def _boolean_tokens(value_format: str) -> tuple[str, str] | None:
    """``"oui/non"`` → ``("oui", "non")``, preserving the sheet's own wording."""
    match = re.fullmatch(r"\s*([^/\s]{1,12})\s*/\s*([^/\s]{1,12})\s*", value_format or "")
    if not match:
        return None
    truthy, falsy = match.group(1), match.group(2)
    if _TRUTHY.get(truthy.casefold()) is True and _TRUTHY.get(falsy.casefold()) is False:
        return truthy, falsy
    return None


_TRUTHY: dict[str, bool] = {
    "true": True,
    "vrai": True,
    "yes": True,
    "y": True,
    "oui": True,
    "o": True,
    "ok": True,
    "1": True,
    "x": True,
    "false": False,
    "faux": False,
    "no": False,
    "n": False,
    "non": False,
    "0": False,
}


def _coerce_boolean(value: Any, tokens: tuple[str, str]) -> str | None:
    """Map any yes/no-ish answer onto the sheet's own tokens, or None if unclear."""
    if isinstance(value, bool):
        return tokens[0] if value else tokens[1]
    decided = _TRUTHY.get(str(value).strip().casefold())
    if decided is None:
        return None
    return tokens[0] if decided else tokens[1]


def _is_datetime(column: SheetColumn) -> bool:
    if column.role == "datetime":
        return True
    return bool(_strftime_pattern(column.value_format))


def _coerce_datetime(value: Any, value_format: str) -> Any:
    """Re-emit a date in the layout the sheet already uses, or leave it alone."""
    raw = str(value).strip()
    pattern = _strftime_pattern(value_format)
    if not raw or not pattern:
        return raw

    parsed = _parse_datetime(raw, order=_date_order(value_format))
    if parsed is None:
        return raw
    return parsed.strftime(pattern)


def _strftime_pattern(value_format: str) -> str:
    """Translate a human format ("YYYY-MM-DD HH:mm") into a strftime pattern.

    "MM" is the month before an hour token has been seen and minutes after it,
    which is how spreadsheet users write both "YYYY-MM-DD" and "HH:MM".
    """
    text = str(value_format or "")
    if not re.search(r"(YYYY|YY|DD|MM)", text):
        return ""

    out: list[str] = []
    index = 0
    seen_hours = False
    while index < len(text):
        chunk = text[index : index + 4]
        if chunk == "YYYY":
            out.append("%Y")
            index += 4
            continue
        pair = text[index : index + 2]
        if pair == "YY":
            out.append("%y")
        elif pair == "DD":
            out.append("%d")
        elif pair == "HH":
            out.append("%H")
            seen_hours = True
        elif pair == "SS" or pair == "ss":
            out.append("%S")
        elif pair.casefold() == "mm":
            out.append("%M" if seen_hours else "%m")
        elif pair == "MI" or pair == "mi":
            out.append("%M")
        else:
            out.append("%%" if text[index] == "%" else text[index])
            index += 1
            continue
        index += 2
    return "".join(out)


def _date_order(value_format: str) -> str:
    """Which of day or month the sheet writes first: the only convention signal.

    "DD/MM/YYYY" says day-first and "MM/DD/YYYY" says month-first, but
    "YYYY-MM-DD" says *nothing* about how a slash-separated input should be read
    — it is the ISO ordering, not a US one. Returning "unknown" there is the
    point: it stops "12/08/2026" from being silently filed as 8 December.
    """
    text = str(value_format or "").upper()
    day = text.find("DD")
    month = text.find("MM")
    if day == -1 or month == -1:
        return "unknown"
    if day < month:
        return "day_first"
    # find("YY") also matches the first "YYYY".
    year = text.find("YY")
    if year != -1 and year < month:
        return "unknown"
    return "month_first"


_DAY_FIRST_PATTERNS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d/%m/%Y %H:%M")
_MONTH_FIRST_PATTERNS = ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m/%d/%Y %H:%M")
_UNAMBIGUOUS_PATTERNS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")


def _parse_datetime(raw: str, *, order: str) -> datetime | None:
    """Parse the shapes a model or a human actually types. Never guesses.

    "03/04/2026" is 3 April or 4 March depending on convention. When the sheet's
    own format states the convention we follow it. When it does not, we accept
    the input only if one reading works and the other cannot ("25/12/2026" can
    only be 25 December); a genuinely ambiguous input returns None and the value
    is written through untouched rather than filed under the wrong month.
    """
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass

    parsed = _first_match(raw, _UNAMBIGUOUS_PATTERNS)
    if parsed is not None:
        return parsed

    day_first = _first_match(raw, _DAY_FIRST_PATTERNS)
    month_first = _first_match(raw, _MONTH_FIRST_PATTERNS)
    if order == "day_first":
        return day_first or month_first
    if order == "month_first":
        return month_first or day_first
    if day_first is not None and month_first is not None and day_first != month_first:
        return None
    return day_first or month_first


def _first_match(raw: str, patterns: Sequence[str]) -> datetime | None:
    for pattern in patterns:
        try:
            return datetime.strptime(raw, pattern)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _normalize_header(header: Any) -> str:
    """Casefolded, whitespace-collapsed, NFKC form used for matching and hashing."""
    text = unicodedata.normalize("NFKC", str(header or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def _normalize_role(role: Any) -> str:
    """Keep catalogue roles, accept clean unknown slugs, reject the rest.

    The catalogue is a guide, not a whitelist — a model that answers
    "appointment_slot" is being more useful than one forced into "other".
    """
    text = str(role or "").strip().casefold().replace(" ", "_").replace("-", "_")
    if text in SHEET_COLUMN_ROLES:
        return text
    if text and len(text) <= _MAX_ROLE_CHARS and re.fullmatch(r"[a-z][a-z0-9_]*", text):
        logger.debug(f"Keeping the off-catalogue sheet column role '{text}'")
        return text
    return _DEFAULT_ROLE


def _clean_text(value: Any, max_chars: int) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _slugify_parameter_name(header: Any) -> str:
    """Header → a function-parameter name the LLM APIs accept.

    Accents are folded rather than dropped ("Téléphone" → "telephone") so the
    parameter still reads as the column it stands for; the untouched header
    travels in the description.
    """
    decomposed = unicodedata.normalize("NFKD", str(header or ""))
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if slug and slug[0].isdigit():
        slug = f"col_{slug}"
    return slug[:_MAX_PARAMETER_NAME_CHARS].strip("_")


def _unique_parameter_name(header: str, letter: str, used: set[str]) -> str:
    """Stable, unique parameter name; falls back to the column letter."""
    base = _slugify_parameter_name(header) or f"column_{letter.lower()}"
    name = base
    suffix = 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = [
    "SHEET_COLUMN_ROLES",
    "SHEET_SCHEMA_VERSION",
    "AmbiguousSheetColumnError",
    "SheetColumn",
    "SheetSchema",
    "SheetSchemaError",
    "UnknownSheetColumnError",
    "coerce_value",
    "headers_fingerprint",
    "infer_sheet_schema",
    "ordered_row_values",
    "schema_is_stale",
    "schema_to_tool_parameters",
    "validate_and_place",
]
