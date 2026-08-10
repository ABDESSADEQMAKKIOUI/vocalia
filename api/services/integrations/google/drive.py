"""Google Drive discovery — the file listings every picker is built on.

Drive is the odd one out among the discovery services: it backs three different
sections of the workspace overview. Spreadsheets and documents are Drive queries
filtered on a mime type (the Sheets and Docs APIs have no list endpoint at all),
and "Drive" itself is the browse view. They all come through one query builder
here so the field mask, paging and ordering cannot drift apart between them.

What a caller actually sees depends on the grant
-----------------------------------------------
This deployment requests ``drive.file`` by default, which does **not** mean "the
whole Drive". It grants per-file access to what the user opened or created with
this application — through the Google Picker, or a file the app itself wrote. So
a listing under ``drive.file`` legitimately returns a handful of files, or none,
on an account holding thousands. That is the scope working as designed, not an
empty Drive and not a bug; the section flags in :mod:`.workspace` are what tell
the owner which of the three it is.

Widening to ``drive.readonly`` returns the whole Drive but makes this a
RESTRICTED scope, with the annual paid security assessment that comes with it —
see :mod:`.scopes`.

Query escaping is a security boundary
-------------------------------------
Drive's ``q`` parameter is a query language, not a search box. A caller-supplied
name flows into it, so an unescaped apostrophe does not merely break the query:
it closes the string literal and lets the rest of the term be read as query
syntax, which is how a search for one folder's contents turns into a listing of
everything the token can see. :func:`_escape_query_value` is the only way a
value is allowed in.
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from api.constants import (
    GOOGLE_DISCOVERY_PAGE_SIZE,
    GOOGLE_DRIVE_API_BASE_URL,
)

from .client import GoogleApiClient, GoogleApiError, GoogleScopeError
from .scopes import SCOPE_DRIVE_FILE, SCOPE_DRIVE_READONLY

SPREADSHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
DOCUMENT_MIME_TYPE = "application/vnd.google-apps.document"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# Requesting only the fields the summaries expose keeps the response small and,
# more importantly, keeps this from quietly depending on data it never asked
# for. Google requires the "files(...)" wrapper — a bare field list returns
# nothing.
_FILE_FIELDS = "nextPageToken,files(id,name,mimeType,webViewLink,modifiedTime)"

# Most recently touched first: for a picker, "what I was just working on" is
# almost always what the owner is looking for.
_ORDER_BY = "modifiedTime desc"

# One Drive listing may span several pages. Bounded so a Drive with 50k files
# cannot turn a picker into a minutes-long crawl; the owner searches instead.
_MAX_PAGES = 5


class GoogleDriveError(GoogleApiError):
    """A Drive call failed."""


class GoogleDriveScopeError(GoogleDriveError, GoogleScopeError):
    """A Drive call was refused for want of a scope.

    Inherits both so ``except GoogleDriveError`` catches it like any other Drive
    failure, while the generic scope handling in the routes still recognises it.
    """


class GoogleDriveClient(GoogleApiClient):
    """Drive v3 access for one organization's connected Google account."""

    base_url = GOOGLE_DRIVE_API_BASE_URL
    api_label = "Google Drive API"
    # drive.file first: it is what this deployment requests by default, so it is
    # the scope an operator is most likely to actually be missing.
    api_scopes = (SCOPE_DRIVE_FILE, SCOPE_DRIVE_READONLY)
    error_class = GoogleDriveError
    scope_error_class = GoogleDriveScopeError

    async def list_files_matching(
        self,
        query: str,
        *,
        limit: Optional[int] = None,
        order_by: Optional[str] = _ORDER_BY,
    ) -> list[dict[str, Any]]:
        """Run one Drive query and return its files as summaries.

        ``query`` is a fully-formed Drive ``q`` expression whose caller-supplied
        parts have already gone through :func:`_escape_query_value`.
        """
        remaining = limit if limit and limit > 0 else None
        page_token: str | None = None
        items: list[dict[str, Any]] = []

        for _ in range(_MAX_PAGES):
            page_size = GOOGLE_DISCOVERY_PAGE_SIZE
            if remaining is not None:
                page_size = min(page_size, remaining)

            params: dict[str, Any] = {
                "q": query,
                "fields": _FILE_FIELDS,
                "pageSize": page_size,
                # Without this, files in a shared drive are invisible even when
                # the token can read them.
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if order_by:
                params["orderBy"] = order_by
            if page_token:
                params["pageToken"] = page_token

            payload = await self._request("GET", "/files", params=params)

            for entry in payload.get("files") or []:
                summary = _as_drive_item(entry)
                if summary is not None:
                    items.append(summary)

            if remaining is not None:
                remaining = max(0, remaining - len(payload.get("files") or []))
                if remaining == 0:
                    break

            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        else:
            logger.warning(
                f"Stopped paging Drive after {_MAX_PAGES} pages for query {query!r}; "
                f"returning the first {len(items)} file(s)"
            )

        return items[:limit] if limit and limit > 0 else items


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


def _escape_query_value(value: str) -> str:
    """Make a caller-supplied value safe inside a single-quoted Drive literal.

    Drive's ``q`` grammar escapes with a backslash, so the backslash itself has
    to go first — escaping the quote before the backslash would let ``\\'`` slip
    through as an escaped backslash followed by a live quote, reopening exactly
    the hole this closes.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _mime_clause(mime_type: str) -> str:
    return f"mimeType = '{_escape_query_value(mime_type)}'"


def _parent_clause(parent_id: str) -> str:
    return f"'{_escape_query_value(parent_id)}' in parents"


def _build_query(*clauses: Optional[str]) -> str:
    """Join clauses, always excluding trashed files.

    Trashed files are excluded everywhere on purpose: a picker that offers a
    file in the bin produces a binding that breaks the moment the bin is emptied.
    """
    parts = ["trashed = false", *[clause for clause in clauses if clause]]
    return " and ".join(parts)


def _as_drive_item(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Narrow one Drive API file to the summary shape, or drop it.

    A file with no id cannot be bound to anything later, so it is dropped rather
    than surfaced as an entry the owner can click and get nothing from.
    """
    file_id = entry.get("id")
    if not file_id:
        return None

    mime_type = entry.get("mimeType")
    return {
        "id": str(file_id),
        # Drive practically always sends a name; the fallback exists because the
        # summary schema requires one and a nameless row would fail validation
        # and take the whole listing down with it.
        "name": entry.get("name") or "Untitled",
        "mime_type": mime_type,
        "is_folder": mime_type == FOLDER_MIME_TYPE,
        "web_url": entry.get("webViewLink"),
        "modified_time": entry.get("modifiedTime"),
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def list_spreadsheets(organization_id: int) -> list[dict[str, Any]]:
    """Spreadsheets the organization's connected account can open.

    Entries match ``SpreadsheetSummary``; the extra keys the Drive summary
    carries are harmless to a Pydantic model that ignores them, and keeping one
    shape means the Drive section and the Sheets section cannot disagree about
    what a file is called.
    """
    client = await GoogleDriveClient.for_organization(organization_id)
    return await client.list_files_matching(_build_query(_mime_clause(SPREADSHEET_MIME_TYPE)))


async def list_documents(organization_id: int) -> list[dict[str, Any]]:
    """Google Docs documents, for :mod:`.docs` to narrow to ``DocsSummary``."""
    client = await GoogleDriveClient.for_organization(organization_id)
    return await client.list_files_matching(_build_query(_mime_clause(DOCUMENT_MIME_TYPE)))


async def list_folders(
    organization_id: int, parent_id: Optional[str] = None
) -> list[dict[str, Any]]:
    """Folders, optionally only those directly inside ``parent_id``.

    ``"root"`` is Drive's own alias for the top of My Drive, so the overview's
    "root folders" needs no special case here.
    """
    client = await GoogleDriveClient.for_organization(organization_id)
    query = _build_query(
        _mime_clause(FOLDER_MIME_TYPE),
        _parent_clause(parent_id) if parent_id else None,
    )
    # Folders read as a browse tree, so alphabetical beats most-recent here.
    return await client.list_files_matching(query, order_by="name")


async def list_files(
    organization_id: int,
    folder_id: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Files, optionally inside one folder and/or of one mime type."""
    client = await GoogleDriveClient.for_organization(organization_id)
    query = _build_query(
        _parent_clause(folder_id) if folder_id else None,
        _mime_clause(mime_type) if mime_type else None,
        # A folder is a file in Drive's model; excluding them keeps "files" and
        # "folders" as the two distinct lists the picker actually shows.
        f"mimeType != '{FOLDER_MIME_TYPE}'" if not mime_type else None,
    )
    return await client.list_files_matching(query)


async def list_recent_files(
    organization_id: int, *, limit: int = 10
) -> list[dict[str, Any]]:
    """The most recently modified files, folders excluded."""
    client = await GoogleDriveClient.for_organization(organization_id)
    query = _build_query(f"mimeType != '{FOLDER_MIME_TYPE}'")
    return await client.list_files_matching(query, limit=limit)


async def search_files(
    organization_id: int,
    query: str,
    mime_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Search by name, optionally narrowed to one mime type.

    Drive's ``contains`` on ``name`` is a prefix-per-word match, not a substring
    one — searching "voice" finds "Voice notes" but not "Invoice". That is
    Drive's behaviour, surfaced as-is rather than papered over with a wildcard
    that Drive would reject.

    An empty term returns nothing rather than everything: a search box the user
    has not typed into should not dump their whole Drive.
    """
    term = (query or "").strip()
    if not term:
        return []

    client = await GoogleDriveClient.for_organization(organization_id)
    drive_query = _build_query(
        f"name contains '{_escape_query_value(term)}'",
        _mime_clause(mime_type) if mime_type else None,
    )
    results = await client.list_files_matching(drive_query)
    logger.debug(
        f"Drive search for {term!r} returned {len(results)} file(s) for organization "
        f"{organization_id}"
    )
    return results


__all__ = [
    "DOCUMENT_MIME_TYPE",
    "FOLDER_MIME_TYPE",
    "GoogleDriveClient",
    "GoogleDriveError",
    "GoogleDriveScopeError",
    "SPREADSHEET_MIME_TYPE",
    "list_documents",
    "list_files",
    "list_folders",
    "list_recent_files",
    "list_spreadsheets",
    "search_files",
]
