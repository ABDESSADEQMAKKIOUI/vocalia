"""Bind a Google Sheet to a tool, and keep its inferred schema fresh.

This is the seam between "the owner picked a spreadsheet" and "the agent has a
tool whose parameters are that spreadsheet's own columns". It reads the header
row, asks :mod:`sheet_schema` what each column means, and stores the result on
the ToolModel so nothing has to be inferred again during a conversation.

The inferred schema is deliberately RETURNED to the caller, not just stored: the
owner has to be able to see what the system understood from their sheet and
correct it. An inference nobody can inspect is an inference nobody can trust.
"""

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from api.db import db_client
from api.enums import ToolCategory
from api.services.integrations.google.sheet_schema import (
    SheetSchema,
    headers_fingerprint,
    infer_sheet_schema,
    schema_is_stale,
    schema_to_tool_parameters,
)
from api.services.integrations.google.sheets_client import GoogleSheetsClient

# Kept in sync with api/services/workflow/tools/custom_tool.py::SHEETS_TOOL_TYPE.
# Imported from there rather than redefined so the two can never drift apart.
from api.services.workflow.tools.custom_tool import SHEETS_TOOL_TYPE

GOOGLE_SHEETS_TOOL_TYPE = SHEETS_TOOL_TYPE

# Enough rows for the model to tell a callback date from a creation date, few
# enough to keep the prompt small and the read cheap.
_SAMPLE_ROWS = 5
_PREVIEW_ROWS_MAX = 25


def _columns_payload(schema: Optional[SheetSchema]) -> List[Dict[str, Any]]:
    """Shape the schema for SheetColumnSchema, the response model the UI reads."""
    if schema is None:
        return []

    # Reuse the same projection the agent's tool parameters come from, so what
    # the owner reviews in the UI is literally what the model will be shown.
    params = {p.get("name"): p for p in schema_to_tool_parameters(schema)}

    payload: List[Dict[str, Any]] = []
    for column in schema.columns:
        param = params.get(column.name, {})
        examples = [column.value_format] if column.value_format else []
        payload.append(
            {
                "header": column.header,
                "parameter_name": column.name,
                "column_letter": column.letter,
                "type": param.get("type", "string"),
                "description": param.get("description") or column.description,
                "required": bool(param.get("required", False)),
                "examples": examples,
            }
        )
    return payload


async def _read_sheet(
    organization_id: int,
    spreadsheet_id: str,
    sheet_name: str,
    header_row: int,
    sample_rows: int,
) -> tuple[List[str], List[str], List[List[Any]], Optional[str], str]:
    """Fetch headers, sample rows and the spreadsheet title in one place."""
    client = await GoogleSheetsClient.for_organization(organization_id)

    headers, letters = await client.get_header_row(
        spreadsheet_id, sheet_name, header_row=header_row
    )
    if not any(str(h).strip() for h in headers):
        raise ValueError(
            f"Row {header_row} of '{sheet_name}' is empty — no column headers to "
            "read. Point header_row at the row that names your columns."
        )

    first_data_row = header_row + 1
    last_row = first_data_row + max(sample_rows, 0) - 1
    rows: List[List[Any]] = []
    if sample_rows > 0:
        rows = await client.read_range(
            spreadsheet_id, f"{sheet_name}!A{first_data_row}:{last_row}"
        )

    title: Optional[str] = None
    try:
        meta = await client.get_spreadsheet_meta(spreadsheet_id)
        title = meta.get("title")
    except Exception as exc:  # noqa: BLE001 - a missing title must not fail a bind
        logger.warning(
            "Could not read spreadsheet title for {}: {}", spreadsheet_id, exc
        )

    return list(headers), list(letters), rows, title, sheet_name


def _definition(
    *,
    spreadsheet_id: str,
    sheet_name: str,
    header_row: int,
    schema: SheetSchema,
    spreadsheet_title: Optional[str],
) -> Dict[str, Any]:
    """The ToolModel.definition a bound sheet produces."""
    return {
        "schema_version": 1,
        "type": GOOGLE_SHEETS_TOOL_TYPE,
        "config": {
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet_title": spreadsheet_title,
            "sheet_name": sheet_name,
            "header_row": header_row,
            # tool_to_function_schema reads this to build one named parameter per
            # column. Without it the tool degrades to a generic values array.
            "sheet_schema": schema.to_dict(),
        },
    }


async def bind_sheet(
    *,
    organization_id: int,
    user_id: int,
    spreadsheet_id: str,
    sheet_name: str,
    tool_name: str,
    header_row: int = 1,
    tool_uuid: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Create (or re-point) a tool backed by one tab of one spreadsheet."""
    headers, _letters, rows, title, sheet_name = await _read_sheet(
        organization_id, spreadsheet_id, sheet_name, header_row, _SAMPLE_ROWS
    )

    schema = await infer_sheet_schema(
        headers,
        rows,
        organization_id,
        sheet_name=sheet_name,
        header_row=header_row,
    )

    definition = _definition(
        spreadsheet_id=spreadsheet_id,
        sheet_name=sheet_name,
        header_row=header_row,
        schema=schema,
        spreadsheet_title=title,
    )

    if tool_uuid:
        tool = await db_client.update_tool(
            tool_uuid=tool_uuid,
            organization_id=organization_id,
            name=tool_name,
            description=description,
            definition=definition,
        )
        if tool is None:
            raise ValueError(f"Tool {tool_uuid} not found")
        created = False
    else:
        tool = await db_client.create_tool(
            organization_id=organization_id,
            user_id=user_id,
            name=tool_name,
            definition=definition,
            category=ToolCategory.INTEGRATION.value,
            description=description,
        )
        created = True

    warnings: List[str] = []
    if schema.degraded_reason:
        # Say it plainly: the columns still work, but their descriptions are the
        # header text rather than an understanding of what belongs in them.
        warnings.append(
            "Column meanings could not be inferred automatically "
            f"({schema.degraded_reason}); each column falls back to its own "
            "header. Review the descriptions before relying on this sheet."
        )

    logger.info(
        "Bound sheet {} tab '{}' to tool {} for org {} ({} columns, source={})",
        spreadsheet_id,
        sheet_name,
        tool.tool_uuid,
        organization_id,
        len(schema.columns),
        schema.source,
    )

    return {
        "tool_uuid": str(tool.tool_uuid),
        "tool_name": tool.name,
        "created": created,
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_title": title,
        "sheet_name": sheet_name,
        "header_row": header_row,
        "header_fingerprint": schema.fingerprint,
        "inferred_at": _parse_inferred_at(schema.inferred_at),
        "columns": _columns_payload(schema),
        "warnings": warnings,
    }


async def refresh_sheet_schema(
    *,
    organization_id: int,
    tool_uuid: str,
    force: bool = False,
) -> Dict[str, Any]:
    """Re-infer a bound sheet's columns when its headers moved.

    Without ``force`` this is a no-op on an unchanged sheet: re-inference costs
    an LLM round trip, and re-running it on identical headers would also churn
    descriptions the owner may have already reviewed.
    """
    tool = await db_client.get_tool_by_uuid(tool_uuid, organization_id)
    if tool is None:
        raise ValueError(f"Tool {tool_uuid} not found")

    definition = tool.definition or {}
    config = definition.get("config", {}) or {}
    spreadsheet_id = config.get("spreadsheet_id")
    sheet_name = config.get("sheet_name")
    header_row = int(config.get("header_row") or 1)
    if not spreadsheet_id or not sheet_name:
        raise ValueError(f"Tool {tool_uuid} is not bound to a spreadsheet")

    cached = SheetSchema.from_dict(config.get("sheet_schema") or {})
    previous_fingerprint = getattr(cached, "fingerprint", None) or None

    headers, _letters, rows, title, sheet_name = await _read_sheet(
        organization_id, spreadsheet_id, sheet_name, header_row, _SAMPLE_ROWS
    )

    stale = schema_is_stale(cached, headers)
    if not stale and not force:
        return {
            "tool_uuid": str(tool.tool_uuid),
            "refreshed": False,
            "header_fingerprint": headers_fingerprint(headers),
            "previous_fingerprint": previous_fingerprint,
            "inferred_at": _parse_inferred_at(getattr(cached, "inferred_at", "")),
            "columns": _columns_payload(cached),
        }

    schema = await infer_sheet_schema(
        headers,
        rows,
        organization_id,
        sheet_name=sheet_name,
        header_row=header_row,
    )

    await db_client.update_tool(
        tool_uuid=tool_uuid,
        organization_id=organization_id,
        definition=_definition(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            header_row=header_row,
            schema=schema,
            spreadsheet_title=title or config.get("spreadsheet_title"),
        ),
    )

    logger.info(
        "Refreshed sheet schema for tool {} (org {}): {} -> {}",
        tool_uuid,
        organization_id,
        previous_fingerprint,
        schema.fingerprint,
    )

    return {
        "tool_uuid": str(tool.tool_uuid),
        "refreshed": True,
        "header_fingerprint": schema.fingerprint,
        "previous_fingerprint": previous_fingerprint,
        "inferred_at": _parse_inferred_at(schema.inferred_at),
        "columns": _columns_payload(schema),
    }


async def preview_sheet(
    *,
    organization_id: int,
    tool_uuid: str,
    row_limit: int = 5,
) -> Dict[str, Any]:
    """Headers, a few rows, and what the system understood — for the owner to check."""
    tool = await db_client.get_tool_by_uuid(tool_uuid, organization_id)
    if tool is None:
        raise ValueError(f"Tool {tool_uuid} not found")

    config = (tool.definition or {}).get("config", {}) or {}
    spreadsheet_id = config.get("spreadsheet_id")
    sheet_name = config.get("sheet_name")
    header_row = int(config.get("header_row") or 1)
    if not spreadsheet_id or not sheet_name:
        raise ValueError(f"Tool {tool_uuid} is not bound to a spreadsheet")

    limit = max(1, min(int(row_limit or 5), _PREVIEW_ROWS_MAX))
    headers, letters, rows, title, sheet_name = await _read_sheet(
        organization_id, spreadsheet_id, sheet_name, header_row, limit
    )

    cached = SheetSchema.from_dict(config.get("sheet_schema") or {})

    return {
        "tool_uuid": str(tool.tool_uuid),
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_title": title or config.get("spreadsheet_title"),
        "sheet_name": sheet_name,
        "header_row": header_row,
        "headers": headers,
        "column_letters": letters,
        "rows": rows,
        "columns": _columns_payload(cached),
        "header_fingerprint": getattr(cached, "fingerprint", None) or None,
        "schema_stale": schema_is_stale(cached, headers),
    }


def _parse_inferred_at(raw: Any) -> Optional[datetime]:
    """SheetSchema stores the timestamp as text; the response model wants datetime."""
    if isinstance(raw, datetime):
        return raw
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "GOOGLE_SHEETS_TOOL_TYPE",
    "bind_sheet",
    "preview_sheet",
    "refresh_sheet_schema",
]
