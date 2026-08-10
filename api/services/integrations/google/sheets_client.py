"""Thin async client for the Google Sheets API v4 (httpx, no SDK).

Only the handful of operations the Sheets tool needs are exposed: read a range,
read the header row, append a row, update named cells of one row, find a row by
a column value, and describe the spreadsheet. Everything returns plain Python
values so callers never have to know the Sheets wire format.

Placement is the server's job
-----------------------------
:meth:`GoogleSheetsClient.update_cells` addresses cells by *column letter*, not
by position in a list: callers resolve a header name to its column letter from
the real header row and hand this client ``{"C": "yes"}``. Unknown or malformed
column keys raise instead of being dropped, so a bad mapping fails loudly rather
than silently writing a value into the wrong column (or nowhere).

Auth and quota
--------------
Bind a client to an organization with :meth:`GoogleSheetsClient.for_organization`
— it pulls a valid access token from ``oauth.get_valid_access_token`` and, if a
request still comes back 401, refreshes once and replays it. The Sheets quota is
per user, per project, per minute, so bursts get 429s: retryable statuses (429,
5xx) and network errors are retried with exponential backoff and jitter,
honouring ``Retry-After`` when Google sends it.

That transport lives in :mod:`.client` and is shared with every other Google API
client; what stays here is what is specific to Sheets — the base URL, the A1
helpers, and the error wording (a 403/404 on Sheets almost always means the file
was never handed to this app through the Picker, which no retry can fix).

Ownership of a spreadsheet id is *not* checked here — the caller must have
resolved it from an organization-scoped record before calling.

Docs: https://developers.google.com/sheets/api/reference/rest
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import quote

from loguru import logger

from api.constants import GOOGLE_SHEETS_API_BASE_URL

from .client import (
    RETRYABLE_STATUSES,
    GoogleApiClient,
    GoogleApiError,
    GoogleScopeError,
)
from .scopes import SCOPE_SPREADSHEETS

# Sheets never goes past column ZZZ in practice (max 18278 columns), so three
# letters is a safe upper bound for a user-supplied column key.
_COLUMN_LETTER_RE = re.compile(r"^[A-Z]{1,3}$")
_CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")

# Values written with USER_ENTERED are parsed the way a human typing into the
# cell would be (dates become dates, "0123" stays text only if the column is
# text). RAW would store everything as a literal string, which makes the sheet
# unusable for formulas and filtering.
_VALUE_INPUT_OPTION = "USER_ENTERED"


class GoogleSheetsError(GoogleApiError):
    """A Google Sheets API request failed.

    ``requires_reconsent`` is set when the failure is an authorization problem
    the user must fix (the grant was revoked, or the file was never granted to
    this app through the Picker) rather than something a retry could resolve.
    """

    default_code = "sheets_error"


class GoogleSheetsScopeError(GoogleScopeError, GoogleSheetsError):
    """Sheets answered 403 insufficient scope.

    Inherits from both hierarchies on purpose: code that handles Sheets errors
    catches it as before, while code that cares about scope gaps in general
    catches it as a ``GoogleScopeError`` and reads ``missing_scope``.
    """

    default_code = "insufficient_scope"


# ---------------------------------------------------------------------------
# A1 notation helpers
# ---------------------------------------------------------------------------


def column_letter(index: int) -> str:
    """0-based column index → A1 column letter (0 → "A", 26 → "AA")."""
    if index < 0:
        raise ValueError("Column index must be zero or positive")
    letters = ""
    current = index
    while True:
        current, remainder = divmod(current, 26)
        letters = chr(ord("A") + remainder) + letters
        if current == 0:
            break
        current -= 1
    return letters


def column_index(letter: str) -> int:
    """A1 column letter → 0-based column index ("A" → 0, "AA" → 26)."""
    normalized = normalize_column_letter(letter)
    index = 0
    for char in normalized:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def normalize_column_letter(letter: str) -> str:
    """Validate and upper-case a column letter, rejecting anything else.

    Guards the write path: a caller that passes a header name, an empty string,
    or "A1" gets an error instead of a silently misplaced value.
    """
    normalized = str(letter or "").strip().upper()
    if not _COLUMN_LETTER_RE.match(normalized):
        raise ValueError(
            f"Invalid spreadsheet column letter: {letter!r} "
            f"(expected something like 'A', 'C' or 'AB')"
        )
    return normalized


def quote_sheet_name(sheet_name: str) -> str:
    """Quote a tab name for A1 notation.

    Sheet names can contain spaces, punctuation and quotes; A1 wraps them in
    single quotes and escapes an embedded quote by doubling it.
    """
    escaped = str(sheet_name).replace("'", "''")
    return f"'{escaped}'"


def build_a1_range(sheet_name: str | None, cells: str = "") -> str:
    """Build a fully-qualified A1 range, e.g. ``'Leads 2026'!A2:C``.

    Omitting ``cells`` yields the whole tab (``'Leads 2026'``).
    """
    if not sheet_name:
        return cells
    if not cells:
        return quote_sheet_name(sheet_name)
    return f"{quote_sheet_name(sheet_name)}!{cells}"


def parse_first_row_number(range_notation: str) -> int | None:
    """Pull the first row number out of a range like ``'Leads'!A7:L7``."""
    cells = str(range_notation or "").split("!")[-1]
    first_cell = cells.split(":")[0].replace("$", "").upper()
    match = _CELL_REF_RE.match(first_cell)
    return int(match.group(2)) if match else None


class GoogleSheetsClient(GoogleApiClient):
    """Access to one Google account's spreadsheets, for one organization."""

    base_url: ClassVar[str] = GOOGLE_SHEETS_API_BASE_URL
    api_label: ClassVar[str] = "Google Sheets API"
    api_scopes: ClassVar[tuple[str, ...]] = (SCOPE_SPREADSHEETS,)
    error_class: ClassVar[type[GoogleApiError]] = GoogleSheetsError
    scope_error_class: ClassVar[type[GoogleApiError]] = GoogleSheetsScopeError

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def read_range(
        self,
        spreadsheet_id: str,
        a1_range: str,
        *,
        value_render_option: str = "FORMATTED_VALUE",
    ) -> list[list[Any]]:
        """Read a range and return its rows.

        Google trims trailing empty rows and trailing empty cells, so rows are
        ragged: row ``i`` may be shorter than row ``i + 1``. Callers that need a
        fixed width should pad against the header length.
        """
        payload = await self._request(
            "GET",
            f"/{quote(spreadsheet_id, safe='')}/values/{quote(a1_range, safe='')}",
            params={
                "majorDimension": "ROWS",
                "valueRenderOption": value_render_option,
            },
        )
        values = payload.get("values") or []
        return [list(row) for row in values]

    async def get_header_row(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        *,
        header_row: int = 1,
    ) -> tuple[list[str], list[str]]:
        """Read a tab's header row and return ``(headers, column_letters)``.

        The two lists are positional: ``headers[i]`` lives in column
        ``column_letters[i]``. Blank cells inside the header are kept as empty
        strings so that alignment is never shifted — dropping them would map
        every following header to the wrong column.
        """
        rows = await self.read_range(
            spreadsheet_id, build_a1_range(sheet_name, f"{header_row}:{header_row}")
        )
        if not rows or not rows[0]:
            raise GoogleSheetsError(
                f"Sheet '{sheet_name}' has no header row at row {header_row}.",
                code="empty_header_row",
            )
        headers = [str(cell).strip() for cell in rows[0]]
        letters = [column_letter(index) for index in range(len(headers))]
        logger.debug(
            f"Read {len(headers)} header cells from spreadsheet {spreadsheet_id} "
            f"tab '{sheet_name}'"
        )
        return headers, letters

    async def get_spreadsheet_meta(self, spreadsheet_id: str) -> dict[str, Any]:
        """Return the spreadsheet title and its tabs.

        A 404 here usually means the file was never granted to this app: the
        ``drive.file`` scope only covers files the user picked through the
        Google Picker.
        """
        payload = await self._request(
            "GET",
            f"/{quote(spreadsheet_id, safe='')}",
            params={
                "fields": (
                    "spreadsheetId,properties.title,"
                    "sheets.properties(sheetId,title,index,"
                    "gridProperties(rowCount,columnCount))"
                )
            },
        )
        sheets = []
        for sheet in payload.get("sheets") or []:
            properties = sheet.get("properties") or {}
            grid = properties.get("gridProperties") or {}
            sheets.append(
                {
                    "sheet_id": properties.get("sheetId"),
                    "title": properties.get("title"),
                    "index": properties.get("index"),
                    "row_count": grid.get("rowCount"),
                    "column_count": grid.get("columnCount"),
                }
            )
        return {
            "spreadsheet_id": payload.get("spreadsheetId") or spreadsheet_id,
            "title": (payload.get("properties") or {}).get("title"),
            "sheets": sheets,
        }

    async def find_row_by_value(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        column: str,
        value: Any,
        *,
        start_row: int = 2,
    ) -> int | None:
        """Return the 1-based row number of the first row whose column matches.

        Comparison is case-insensitive and whitespace-trimmed, which is what
        matching a spoken value ("john@example.com " vs "John@Example.com")
        needs. ``start_row`` defaults to 2 to skip the header. Returns None when
        nothing matches.

        This reads the whole column: Sheets has no server-side search, so cost
        grows with the sheet. Callers on the live call path should keep sheets
        reasonably sized or cache the lookup.
        """
        letter = normalize_column_letter(column)
        if start_row < 1:
            raise ValueError("start_row must be 1 or greater")

        rows = await self.read_range(
            spreadsheet_id, build_a1_range(sheet_name, f"{letter}{start_row}:{letter}")
        )
        needle = str(value).strip().casefold()
        for offset, row in enumerate(rows):
            cell = row[0] if row else ""
            if str(cell).strip().casefold() == needle:
                return start_row + offset
        return None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def append_row(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        ordered_values: list[Any],
    ) -> dict[str, Any]:
        """Append one row at the bottom of the tab's data.

        ``ordered_values`` is positional — index ``i`` lands in the column of
        ``headers[i]``. Build it from the header row, padding with empty strings
        for columns the caller has no value for; never from an arbitrary dict
        ordering.

        Returns ``{"updated_range", "row_number", "updated_cells"}``;
        ``row_number`` is the 1-based row Google actually wrote to.
        """
        if not isinstance(ordered_values, list) or not ordered_values:
            raise ValueError("ordered_values must be a non-empty list of cell values")

        # The append range is the whole tab, not "A1": Google appends after the
        # last row of the *table* containing the range, so anchoring on A1 would
        # stop at the first blank row and overwrite whatever follows it.
        payload = await self._request(
            "POST",
            (
                f"/{quote(spreadsheet_id, safe='')}/values/"
                f"{quote(build_a1_range(sheet_name), safe='')}:append"
            ),
            params={
                "valueInputOption": _VALUE_INPUT_OPTION,
                "insertDataOption": "INSERT_ROWS",
                "includeValuesInResponse": "false",
            },
            json_body={"values": [ordered_values]},
        )
        updates = payload.get("updates") or {}
        updated_range = updates.get("updatedRange") or ""
        row_number = parse_first_row_number(updated_range)
        logger.info(
            f"Appended a row to spreadsheet {spreadsheet_id} tab '{sheet_name}' "
            f"(range={updated_range}, cells={updates.get('updatedCells')})"
        )
        return {
            "updated_range": updated_range,
            "row_number": row_number,
            "updated_cells": updates.get("updatedCells"),
        }

    async def update_cells(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        row_number: int,
        values_by_column: dict[str, Any],
    ) -> dict[str, Any]:
        """Write specific cells of one existing row.

        ``values_by_column`` maps column letters to values (``{"C": "yes"}``).
        Every key is validated: an invalid column letter raises ``ValueError``
        rather than being skipped, so a caller that mapped a header name badly
        finds out immediately.

        Sent as a single ``values:batchUpdate`` so the whole row updates
        atomically from the sheet's point of view and costs one quota unit
        instead of one per column.
        """
        if row_number < 1:
            raise ValueError("row_number must be 1 or greater")
        if not values_by_column:
            raise ValueError("update_cells needs at least one column to write")

        data = []
        seen: set[str] = set()
        for raw_letter, value in values_by_column.items():
            letter = normalize_column_letter(raw_letter)
            if letter in seen:
                # Two keys normalizing to the same column ("c" and "C") would
                # both be written and the last one would silently win.
                raise ValueError(
                    f"Column {letter} was given more than one value to write"
                )
            seen.add(letter)
            data.append(
                {
                    "range": build_a1_range(sheet_name, f"{letter}{row_number}"),
                    "values": [[value]],
                }
            )

        payload = await self._request(
            "POST",
            f"/{quote(spreadsheet_id, safe='')}/values:batchUpdate",
            json_body={"valueInputOption": _VALUE_INPUT_OPTION, "data": data},
        )
        logger.info(
            f"Updated {len(data)} cell(s) on row {row_number} of spreadsheet "
            f"{spreadsheet_id} tab '{sheet_name}'"
        )
        return {
            "updated_cells": payload.get("totalUpdatedCells"),
            "updated_ranges": [
                item.get("updatedRange") for item in payload.get("responses") or []
            ],
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _translate_error(
        self,
        *,
        status_code: int,
        message: str,
        reason: str,
        method: str,
        path: str,
    ) -> GoogleApiError:
        """Sheets-specific wording for a failed request."""
        if status_code in (401, 403):
            # 403 on Sheets is normally "the file was never shared with this
            # app" — with drive.file that means it was not picked, so a plain
            # retry cannot help. (A 403 that really is a scope gap never reaches
            # here: the transport turns it into GoogleSheetsScopeError.)
            return GoogleSheetsError(
                message
                or "Google denied access to this spreadsheet. Re-pick the file "
                "from Google Drive to grant access.",
                code=reason or "permission_denied",
                http_status=status_code,
                requires_reconsent=status_code == 401,
            )
        if status_code == 404:
            return GoogleSheetsError(
                message
                or "Spreadsheet not found, or it was never granted to this app.",
                code=reason or "not_found",
                http_status=status_code,
            )

        logger.error(
            f"Google Sheets API {method} {path} failed with HTTP {status_code}: "
            f"{message}"
        )
        return GoogleSheetsError(
            message or f"Google Sheets API request failed with HTTP {status_code}.",
            code=reason or "sheets_error",
            http_status=status_code,
            retryable=status_code in RETRYABLE_STATUSES,
        )
