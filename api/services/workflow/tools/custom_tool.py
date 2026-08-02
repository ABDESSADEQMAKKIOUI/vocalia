"""Custom tool execution for user-defined HTTP API tools."""

import json
import re
import unicodedata
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Iterable, List, Optional

import httpx
from loguru import logger

from api.db import db_client
from api.utils.credential_auth import build_auth_header
from api.utils.template_renderer import render_template

# Map tool parameter types to JSON schema types
TYPE_MAP = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}

# ---------------------------------------------------------------------------
# Google Sheets: parameters derived from the bound sheet, not from config
# ---------------------------------------------------------------------------
#
# A Sheets tool has no per-column configuration. When the sheet is bound, the
# meaning of every column is inferred once and cached in
# ``config["sheet_schema"]`` against a fingerprint of the header row. This
# module turns that cache into one named, described LLM parameter per column:
# a model places a value far more reliably in `email_address` ("the customer's
# e-mail address") than in the 7th slot of an anonymous array.
#
# Placement stays server-side. The model only ever sends
# ``{parameter_name: value}``; the executor maps parameter names back to real
# headers with :func:`sheets_column_parameter_map` and lets the Sheets client
# resolve column letters. Nothing here trusts the model with a cell address.

SHEETS_TOOL_TYPE = "google_sheets"

SHEETS_OPERATIONS = ("read_rows", "append_row", "update_row")
SHEETS_WRITE_OPERATIONS = ("append_row", "update_row")

SHEETS_OPERATION_PARAM = "operation"
SHEETS_MATCH_COLUMN_PARAM = "match_column"
SHEETS_MATCH_VALUE_PARAM = "match_value"
SHEETS_MAX_ROWS_PARAM = "max_rows"
# Degraded mode only: used when no column inference has been cached yet.
SHEETS_VALUES_PARAM = "values"

# Parameter names the tool owns; a column that sanitizes to one of these is
# suffixed instead of shadowing it.
SHEETS_RESERVED_PARAMS = frozenset(
    {
        SHEETS_OPERATION_PARAM,
        SHEETS_MATCH_COLUMN_PARAM,
        SHEETS_MATCH_VALUE_PARAM,
        SHEETS_MAX_ROWS_PARAM,
        SHEETS_VALUES_PARAM,
    }
)

# Inferred column types are a richer vocabulary than JSON schema's. Anything
# unknown degrades to "string": Sheets parses values with USER_ENTERED anyway,
# so a wrong-but-plausible string still lands correctly, while a wrong "number"
# would make the model drop a value it could not coerce.
SHEETS_COLUMN_TYPE_MAP = {
    "string": "string",
    "text": "string",
    "number": "number",
    "integer": "number",
    "float": "number",
    "currency": "number",
    "percent": "number",
    "boolean": "boolean",
    "bool": "boolean",
}

_SHEETS_OPERATION_HELP = {
    "read_rows": "'read_rows' returns rows that are already in the sheet",
    "append_row": "'append_row' adds a new row at the bottom of the sheet",
    "update_row": "'update_row' changes values on one existing row",
}

# Candidate keys accepted from the cached inference, so a field rename in the
# inference module does not silently produce an empty parameter list.
# "header" is tried FIRST on purpose. SheetColumn.to_dict emits both: "header"
# is the real column text ("Prénom du client") and "name" is the slug generated
# for the LLM parameter ("prenom_du_client"). Placement happens by header, so
# reading "name" first made the resolver map a parameter back to its own slug —
# no live header ever matched it, and every read filter silently returned zero
# rows. The remaining keys keep hand-written schemas working.
_SHEETS_NAME_KEYS = ("header", "name", "header_name", "title", "label")
_SHEETS_DESCRIPTION_KEYS = ("description", "meaning", "summary")
_SHEETS_TYPE_KEYS = ("type", "value_type", "data_type")
_SHEETS_EXAMPLE_KEYS = ("examples", "sample_values", "samples")
_SHEETS_ALLOWED_VALUE_KEYS = ("allowed_values", "enum", "options")


def _first_value(entry: Dict[str, Any], keys: Iterable[str]) -> Any:
    """Return the first non-empty value among ``keys``."""
    for key in keys:
        value = entry.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _as_plain_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a cached inference fragment into a plain dict, or None."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value if isinstance(value, dict) else None


def sheets_cached_schema(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return the cached column inference stored on a Sheets tool.

    Tolerates the shapes the cache can legitimately arrive in: the dataclass
    itself (in-process), the JSON dict it serializes to (round-tripped through
    the database), a JSON string, or a bare list of columns. Returns ``{}`` when
    the sheet has never been inferred — callers degrade, they do not raise.
    """
    raw = config.get("sheet_schema")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Sheets tool has an unparseable cached sheet_schema")
            return {}
    if isinstance(raw, list):
        return {"columns": raw}
    return _as_plain_dict(raw) or {}


def sheets_schema_columns(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize the cached inference into ``{name, description, ...}`` entries.

    Column letters are deliberately not carried through: resolving a header to a
    cell is the placement step's job, never this module's.
    """
    raw_columns = sheets_cached_schema(config).get("columns")
    if isinstance(raw_columns, dict):
        # {"Email": {...}} form — the key is the header.
        raw_columns = [
            {**(_as_plain_dict(entry) or {}), "name": name}
            for name, entry in raw_columns.items()
        ]
    if not isinstance(raw_columns, list):
        return []

    columns: List[Dict[str, Any]] = []
    for raw_column in raw_columns:
        entry = _as_plain_dict(raw_column)
        if entry is None:
            continue
        name = _first_value(entry, _SHEETS_NAME_KEYS)
        if not name or not str(name).strip():
            # A blank header carries no meaning the model could act on, and its
            # position is already preserved by the inference cache.
            continue
        examples = _first_value(entry, _SHEETS_EXAMPLE_KEYS)
        allowed_values = _first_value(entry, _SHEETS_ALLOWED_VALUE_KEYS)
        columns.append(
            {
                "name": str(name).strip(),
                "description": str(_first_value(entry, _SHEETS_DESCRIPTION_KEYS) or ""),
                "type": str(_first_value(entry, _SHEETS_TYPE_KEYS) or "string").lower(),
                "examples": [str(item) for item in examples]
                if isinstance(examples, (list, tuple))
                else [],
                "allowed_values": [str(item) for item in allowed_values]
                if isinstance(allowed_values, (list, tuple))
                else [],
            }
        )
    return columns


def sheets_parameter_name(
    column_name: str, taken: Optional[Iterable[str]] = None
) -> str:
    """Turn a column header into a function-parameter name.

    "Prénom du client" becomes ``prenom_du_client``: accents are folded and
    anything that is not ``[a-z0-9_]`` becomes an underscore, because function
    parameter names have to survive every LLM provider's validation. The mapping
    is deterministic, so the executor rebuilds it from the same cache to
    translate what the model sent back into real headers.
    """
    folded = unicodedata.normalize("NFKD", str(column_name or ""))
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    base = re.sub(r"[^a-z0-9_]", "_", folded.lower())
    base = re.sub(r"_+", "_", base).strip("_")
    if not base or base[0].isdigit():
        # Headers like "#" or "2026 target" would otherwise produce a name no
        # provider accepts.
        base = f"column_{base}" if base else "column"

    reserved = set(SHEETS_RESERVED_PARAMS) | set(taken or ())
    candidate = base
    suffix = 2
    while candidate in reserved:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def sheets_column_parameter_map(config: Dict[str, Any]) -> Dict[str, str]:
    """Map every generated parameter name back to its real column header.

    The single source of truth shared by the schema side (which names the
    parameters) and the execution side (which resolves them back to headers).

    A header that appears twice yields a single parameter: values are placed by
    header name, so a second parameter for the same header would be a promise
    the write path cannot keep — the model would fill in a field whose value
    silently overwrites the other one.
    """
    mapping: Dict[str, str] = {}
    seen_headers = set()
    for column in sheets_schema_columns(config):
        header = column["name"]
        if header in seen_headers:
            continue
        seen_headers.add(header)
        mapping[sheets_parameter_name(header, taken=mapping.keys())] = header
    return mapping


def sheets_allowed_operations(config: Dict[str, Any]) -> List[str]:
    """Operations this tool may perform, in canonical order.

    An unusable or missing restriction falls back to all three rather than
    leaving the agent with a tool it cannot call at all.
    """
    configured = config.get("allowed_operations") or config.get("operations")
    if not isinstance(configured, (list, tuple)):
        return list(SHEETS_OPERATIONS)
    requested = {str(item) for item in configured}
    allowed = [operation for operation in SHEETS_OPERATIONS if operation in requested]
    return allowed or list(SHEETS_OPERATIONS)


def _sheets_column_property(column: Dict[str, Any]) -> Dict[str, Any]:
    """Build one JSON schema property from an inferred column."""
    description = column["description"] or (
        f"Value for the '{column['name']}' column of the spreadsheet."
    )
    if column["examples"]:
        description = f"{description} Examples: {', '.join(column['examples'])}."

    schema_type = SHEETS_COLUMN_TYPE_MAP.get(column["type"], "string")
    prop: Dict[str, Any] = {"type": schema_type, "description": description}
    if column["allowed_values"] and schema_type == "string":
        prop["enum"] = column["allowed_values"]
    return prop


def _apply_sheets_parameters(
    config: Dict[str, Any],
    properties: Dict[str, Any],
    required: List[str],
) -> None:
    """Expose the bound sheet's columns as named parameters.

    Only ``operation`` is required: which columns the agent can fill in depends
    on the operation it picks, and JSON schema cannot express that. The executor
    enforces the per-operation rules and reports violations back to the agent.
    """
    operations = sheets_allowed_operations(config)
    writes = [op for op in operations if op in SHEETS_WRITE_OPERATIONS]
    column_params = sheets_column_parameter_map(config)

    columns_by_name: Dict[str, Dict[str, Any]] = {}
    for column in sheets_schema_columns(config):
        # First occurrence wins, matching the parameter map.
        columns_by_name.setdefault(column["name"], column)

    operation_description = "What to do with the spreadsheet: " + "; ".join(
        _SHEETS_OPERATION_HELP[operation] for operation in operations
    )
    if writes:
        operation_description += (
            ". The column parameters below are the values to write; "
            "they are ignored by 'read_rows'"
        )
    properties[SHEETS_OPERATION_PARAM] = {
        "type": "string",
        "enum": operations,
        "description": f"{operation_description}.",
    }
    if SHEETS_OPERATION_PARAM not in required:
        required.append(SHEETS_OPERATION_PARAM)

    if writes:
        if column_params:
            for param_name, header in column_params.items():
                properties[param_name] = _sheets_column_property(
                    columns_by_name[header]
                )
        else:
            # No inference cached yet (sheet just bound, or headers changed and
            # re-inference has not run). Degrade to a free-form map instead of
            # failing schema generation mid-call: the executor validates the
            # keys against the sheet's real headers before writing anything.
            properties[SHEETS_VALUES_PARAM] = {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Values to write, keyed by the exact column header they "
                    'belong to, e.g. {"First name": "Ada"}. Unknown headers are '
                    "rejected."
                ),
            }

    if "update_row" in operations or "read_rows" in operations:
        match_description = (
            "Column used to find the row to act on. Required with 'update_row'; "
            "with 'read_rows' it filters the rows returned"
        )
        match_property: Dict[str, Any] = {"type": "string"}
        if column_params:
            match_property["enum"] = list(column_params)
        else:
            match_description += " (use the exact column header)"
        match_property["description"] = f"{match_description}."
        properties[SHEETS_MATCH_COLUMN_PARAM] = match_property
        properties[SHEETS_MATCH_VALUE_PARAM] = {
            "type": "string",
            "description": (
                "Value to look for in 'match_column'. Matching ignores case and "
                "surrounding spaces."
            ),
        }

    if "read_rows" in operations:
        properties[SHEETS_MAX_ROWS_PARAM] = {
            "type": "number",
            "description": "Maximum number of rows 'read_rows' should return.",
        }


def tool_to_function_schema(tool: Any) -> Dict[str, Any]:
    """Convert a ToolModel to an LLM function schema.

    Args:
        tool: ToolModel instance with name, description, and definition

    Returns:
        Function schema dict compatible with OpenAI/Anthropic function calling
    """
    definition = tool.definition or {}
    config = definition.get("config", {})
    parameters = config.get("parameters", []) or []
    if (
        definition.get("type") == "transfer_call"
        and config.get("destination_source", "static") != "dynamic"
    ):
        parameters = []
    elif (
        definition.get("type") == "transfer_call"
        and config.get("destination_source", "static") == "dynamic"
    ):
        resolver = config.get("resolver")
        if isinstance(resolver, dict):
            parameters = resolver.get("parameters", []) or []
        else:
            parameters = []
    elif definition.get("type") == SHEETS_TOOL_TYPE:
        # Sheets tools declare no static parameter list: their parameters are
        # the columns of the bound sheet, built below from the cached
        # inference. Ignore anything left in config["parameters"] so a stale
        # hand-written list cannot leak into the schema.
        parameters = []

    # Build properties and required list from parameters
    properties = {}
    required = []

    for param in parameters:
        param_name = param.get("name", "")
        param_type = param.get("type", "string")
        param_desc = param.get("description", "")
        param_required = param.get("required", True)

        if not param_name:
            continue

        schema_type = TYPE_MAP.get(param_type, "string")
        if schema_type == "object":
            properties[param_name] = {
                "type": "object",
                "additionalProperties": True,
                "description": param_desc,
            }
        elif schema_type == "array":
            properties[param_name] = {
                "type": "array",
                "items": {},
                "description": param_desc,
            }
        else:
            properties[param_name] = {
                "type": schema_type,
                "description": param_desc,
            }

        if param_required:
            required.append(param_name)

    # If this is an end_call tool with endCallReason enabled, add a required 'reason' parameter
    if definition.get("type") == "end_call" and config.get("endCallReason", False):
        default_description = (
            "The reason for ending the call (e.g., 'voicemail_detected', "
            "'issue_resolved', 'customer_requested')"
        )
        properties["reason"] = {
            "type": "string",
            "description": config.get("endCallReasonDescription")
            or default_description,
        }
        required.append("reason")

    # Google Sheets tools expose one parameter per column of the bound sheet,
    # named and described from the inference cached when the sheet was linked.
    if definition.get("type") == SHEETS_TOOL_TYPE:
        _apply_sheets_parameters(config, properties, required)

    # Sanitize tool name for function name (lowercase, underscores only)
    function_name = re.sub(r"[^a-z0-9_]", "_", tool.name.lower())
    # Remove consecutive underscores and trim
    function_name = re.sub(r"_+", "_", function_name).strip("_")

    return {
        "type": "function",
        "function": {
            "name": function_name,
            "description": tool.description or f"Execute {tool.name} tool",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        "_tool_uuid": tool.tool_uuid,
    }


def _coerce_parameter_value(value: Any, param_type: str) -> Any:
    """Coerce a rendered preset parameter into the configured JSON type."""

    if value is None:
        return None

    if param_type == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    if param_type == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value

        rendered = str(value).strip()
        if rendered == "":
            return None

        if re.fullmatch(r"[-+]?\d+", rendered):
            return int(rendered)

        return float(rendered)

    if param_type == "boolean":
        if isinstance(value, bool):
            return value

        if isinstance(value, (int, float)):
            return bool(value)

        rendered = str(value).strip().lower()
        if rendered in {"true", "1", "yes", "y", "on"}:
            return True
        if rendered in {"false", "0", "no", "n", "off"}:
            return False

        raise ValueError(f"Cannot convert '{value}' to boolean")

    if param_type == "object":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Cannot convert '{value}' to object") from exc
        if isinstance(value, dict):
            return value
        raise ValueError(f"Cannot convert '{value}' to object")

    if param_type == "array":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Cannot convert '{value}' to array") from exc
        if isinstance(value, list):
            return value
        raise ValueError(f"Cannot convert '{value}' to array")

    return value


def _resolve_preset_parameters(
    config: Dict[str, Any],
    call_context_vars: Optional[Dict[str, Any]],
    gathered_context_vars: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve fixed/template-backed parameters before executing the HTTP request."""

    preset_parameters = config.get("preset_parameters", []) or []
    if not preset_parameters:
        return {}

    initial_context = dict(call_context_vars or {})
    render_context: Dict[str, Any] = {
        **initial_context,
        "initial_context": initial_context,
        "gathered_context": dict(gathered_context_vars or {}),
    }

    resolved: Dict[str, Any] = {}
    for param in preset_parameters:
        param_name = (param.get("name") or "").strip()
        if not param_name:
            continue

        rendered = render_template(param.get("value_template", ""), render_context)
        if rendered in (None, ""):
            if param.get("required", True):
                raise ValueError(
                    f"Preset parameter '{param_name}' resolved to an empty value"
                )
            continue

        resolved[param_name] = _coerce_parameter_value(
            rendered, param.get("type", "string")
        )

    return resolved


async def execute_http_tool(
    tool: Any,
    arguments: Dict[str, Any],
    call_context_vars: Optional[Dict[str, Any]] = None,
    gathered_context_vars: Optional[Dict[str, Any]] = None,
    organization_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute an HTTP API tool.

    Args:
        tool: ToolModel instance
        arguments: Arguments passed by the LLM (parameter name -> value)
        call_context_vars: Initial context variables available at runtime
        gathered_context_vars: Variables extracted during the conversation
        organization_id: Organization ID for credential lookup

    Returns:
        Result dict with response data or error
    """
    definition = tool.definition or {}
    config = definition.get("config", {})

    # Get HTTP method and URL
    method = config.get("method", "POST").upper()
    url = config.get("url", "")

    # Get headers from config
    headers = dict(config.get("headers", {}) or {})

    # Add auth header if credential is configured
    credential_uuid = config.get("credential_uuid")
    if credential_uuid and organization_id:
        try:
            credential = await db_client.get_credential_by_uuid(
                credential_uuid, organization_id
            )
            if credential:
                auth_header = build_auth_header(credential)
                headers.update(auth_header)
                logger.debug(f"Applied credential '{credential.name}' to tool request")
            else:
                logger.warning(
                    f"Credential {credential_uuid} not found for tool '{tool.name}'"
                )
        except Exception as e:
            logger.error(f"Failed to fetch credential for tool '{tool.name}': {e}")

    # Get timeout
    timeout_ms = config.get("timeout_ms", 5000)
    timeout_seconds = timeout_ms / 1000

    try:
        preset_arguments = _resolve_preset_parameters(
            config, call_context_vars, gathered_context_vars
        )
    except ValueError as e:
        logger.error(f"Custom tool '{tool.name}' preset parameter error: {e}")
        return {"status": "error", "error": str(e)}

    resolved_arguments = {**(arguments or {}), **preset_arguments}

    # Build request: JSON body for POST/PUT/PATCH, query params for GET/DELETE
    body = None
    params = None
    if method in ("POST", "PUT", "PATCH"):
        body = resolved_arguments
    elif method in ("GET", "DELETE") and resolved_arguments:
        params = resolved_arguments

    logger.info(
        f"Executing custom tool '{tool.name}' ({tool.tool_uuid}): {method} {url}"
    )
    if preset_arguments:
        logger.debug(
            f"Resolved preset parameters for '{tool.name}': {list(preset_arguments.keys())}"
        )
    logger.debug(f"Request body: {body}, params: {params}")

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=body,
                params=params,
            )

            # Try to parse JSON response
            try:
                response_data = response.json()
            except Exception:
                response_data = {"raw_response": response.text}

            result = {
                "status": "success",
                "status_code": response.status_code,
                "data": response_data,
            }

            logger.debug(
                f"Custom tool '{tool.name}' completed with status {response.status_code}"
            )
            return result

    except httpx.TimeoutException:
        logger.error(f"Custom tool '{tool.name}' timed out after {timeout_seconds}s")
        return {
            "status": "error",
            "error": f"Request timed out after {timeout_seconds} seconds",
        }
    except httpx.RequestError as e:
        logger.error(f"Custom tool '{tool.name}' request failed: {e}")
        return {
            "status": "error",
            "error": f"Request failed: {str(e)}",
        }
    except Exception as e:
        logger.error(f"Custom tool '{tool.name}' execution failed: {e}")
        return {
            "status": "error",
            "error": f"Tool execution failed: {str(e)}",
        }
