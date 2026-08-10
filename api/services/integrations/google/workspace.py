"""Workspace discovery — what a connected Google account actually contains.

Right after the consent redirect the UI asks one question ("what did I just
connect, and what can it do?") whose honest answer spans five Google APIs.
Asking them one at a time costs five serial round-trips, and — worse — returns
five empty lists the customer cannot tell apart: an empty Calendar section means
either "you have no calendars" or "you never granted Calendar", and those need
opposite buttons.

Three rules shape this module:

- **Scopes decide before Google does.** A section whose scopes this
  organization's grant does not cover is answered from the registry in
  :mod:`.scopes`, with no HTTP call at all. Sending a request that is guaranteed
  to come back 403 buys nothing that was not already known.
- **Sections fail alone.** Everything runs under one ``asyncio.gather`` with
  ``return_exceptions=True``: Gmail being down leaves the other four intact and
  names the failure in ``gmail.error``.
- **Empty is never silent.** Every section carries ``available`` /
  ``scope_missing`` / ``missing_scopes`` / ``error``, which is the whole contract
  of ``WorkspaceSectionFlags``.

Nothing here checks ownership of a resource id: the organization is the unit of
access, its access token is resolved from its own stored connection, and Google
enforces the rest. Callers must still pass an ``organization_id`` they resolved
from the authenticated user — never one from a request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from loguru import logger

from api.constants import (
    GOOGLE_DISCOVERY_RECENT_FILES,
    GOOGLE_DISCOVERY_RECENT_MESSAGES,
)

from . import calendar as calendar_api
from . import docs, drive, gmail
from .client import GoogleApiError
from .oauth import GoogleOAuthError, get_connection
from .scopes import (
    FAMILY_CALENDAR,
    FAMILY_DOCS,
    FAMILY_DRIVE,
    FAMILY_GMAIL,
    FAMILY_SHEETS,
    SCOPE_DRIVE_READONLY,
    family_scopes,
    has_scope,
    missing_family_scopes,
    normalize_scopes,
    requested_scopes,
)
from .sheets_client import GoogleSheetsClient, build_a1_range

ItemT = TypeVar("ItemT")

# A preview is a glance, not an export: an unbounded range on a wide sheet is
# megabytes of cells nobody reads, paid for in latency and memory.
PREVIEW_DEFAULT_CELLS = "A1:J20"


@dataclass(frozen=True)
class _SectionSpec:
    """One overview section: which scopes it needs, and how to fill it."""

    name: str
    family: str
    # Keys the section's payload carries, so an unavailable or failed section
    # still answers with the same shape as a successful one.
    keys: tuple[str, ...]
    fetch: Callable[[int], Awaitable[dict[str, list[Any]]]]
    # True when filling this section means enumerating Drive. Those sections
    # succeed under drive.file and still come back nearly empty, because that
    # scope only sees files this application was given — which is a different
    # thing from "the account has none" and has to be said out loud.
    enumerates_drive: bool = False

    def empty(self) -> dict[str, list[Any]]:
        return {key: [] for key in self.keys}


# Said once, in the owner's terms rather than Google's: "drive.file" means
# nothing to the person reading it, "the files you have picked" does.
_DRIVE_FILE_NOTICE = (
    "This deployment only asks for access to files you explicitly pick, so this "
    "list shows the files already shared with Volira — not everything in your "
    "Drive. Pick a file to add it."
)


# ---------------------------------------------------------------------------
# Section fetchers
# ---------------------------------------------------------------------------


async def _fetch_sheets(organization_id: int) -> dict[str, list[Any]]:
    return {"items": await drive.list_spreadsheets(organization_id)}


async def _fetch_calendars(organization_id: int) -> dict[str, list[Any]]:
    return {"items": await calendar_api.list_calendars(organization_id)}


async def _fetch_drive(organization_id: int) -> dict[str, list[Any]]:
    # Two independent Drive queries — "what changed lately" and "where can I
    # browse from" — so they run together rather than one after the other.
    recent_files, root_folders = await asyncio.gather(
        drive.list_recent_files(organization_id, limit=GOOGLE_DISCOVERY_RECENT_FILES),
        drive.list_folders(organization_id, parent_id="root"),
    )
    return {"recent_files": recent_files, "root_folders": root_folders}


async def _fetch_gmail(organization_id: int) -> dict[str, list[Any]]:
    labels, recent = await asyncio.gather(
        gmail.list_labels(organization_id),
        gmail.list_messages(
            organization_id,
            label_id="INBOX",
            max_results=GOOGLE_DISCOVERY_RECENT_MESSAGES,
        ),
    )
    return {"labels": labels, "recent": recent}


async def _fetch_docs(organization_id: int) -> dict[str, list[Any]]:
    return {"items": await docs.list_documents(organization_id)}


_SECTIONS: tuple[_SectionSpec, ...] = (
    # Sheets, Drive and Docs all enumerate through Drive's files.list — the
    # Sheets and Docs APIs have no listing of their own — so all three are
    # narrowed by a drive.file grant.
    _SectionSpec("sheets", FAMILY_SHEETS, ("items",), _fetch_sheets, True),
    _SectionSpec("calendars", FAMILY_CALENDAR, ("items",), _fetch_calendars),
    _SectionSpec(
        "drive", FAMILY_DRIVE, ("recent_files", "root_folders"), _fetch_drive, True
    ),
    _SectionSpec("gmail", FAMILY_GMAIL, ("labels", "recent"), _fetch_gmail),
    _SectionSpec("docs", FAMILY_DOCS, ("items",), _fetch_docs, True),
)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


async def build_overview(organization_id: int) -> dict[str, Any]:
    """Everything the organization's Google account exposes, in one round-trip.

    Shaped for ``WorkspaceOverviewResponse``. Never raises for a missing or
    revoked connection — that is reported as ``connected: false`` with empty
    sections, because "not connected" is a state the overview exists to
    describe, not an error.
    """
    connection = await get_connection(organization_id)
    connected = connection is not None and not connection.is_revoked

    overview: dict[str, Any] = {
        "connected": connected,
        "google_email": connection.account_email if connection else None,
        "status": connection.status if connection else None,
        "requires_reconsent": bool(connection and connection.is_revoked),
        "granted_scopes": [],
        "requested_scopes": list(requested_scopes()),
    }

    if not connected:
        # Flags stay clean: the fix is "connect Google", and a section flagged
        # scope_missing here would send the UI asking for a permission instead.
        overview.update({spec.name: spec.empty() for spec in _SECTIONS})
        return overview

    granted = normalize_scopes(connection.scopes)
    overview["granted_scopes"] = list(granted)

    plans = [(spec, missing_family_scopes(granted, spec.family)) for spec in _SECTIONS]
    reachable = [spec for spec, missing in plans if not missing]

    # The whole point of the endpoint: total latency is the slowest section, not
    # the sum of all five.
    results = await asyncio.gather(
        *(spec.fetch(organization_id) for spec in reachable),
        return_exceptions=True,
    )
    fetched = {
        spec.name: result for spec, result in zip(reachable, results, strict=True)
    }

    for spec, missing in plans:
        if missing:
            overview[spec.name] = {
                **spec.empty(),
                "available": False,
                "scope_missing": True,
                "missing_scopes": list(missing),
            }
            continue
        overview[spec.name] = _section_from_result(
            spec, fetched[spec.name], organization_id, granted=granted
        )

    return overview


def _section_from_result(
    spec: _SectionSpec,
    result: Any,
    organization_id: int,
    *,
    granted: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Turn one gathered outcome into a section, payload or explanation."""
    if isinstance(result, BaseException):
        if not isinstance(result, Exception):
            # KeyboardInterrupt / CancelledError are not this layer's to absorb.
            raise result
        return {**spec.empty(), **_failure_flags(spec, result, organization_id)}

    return {
        **spec.empty(),
        **result,
        "available": True,
        "scope_missing": False,
        "missing_scopes": [],
        "notice": _grant_notice(spec, granted),
    }


def _grant_notice(spec: _SectionSpec, granted: tuple[str, ...]) -> str | None:
    """Explain a short list that is neither a scope gap nor a failure.

    ``scope_missing`` would be a lie here — the scopes the section declares ARE
    granted and the call succeeded. What is narrow is what drive.file can see.
    Saying nothing leaves the owner with "available, no error, no items", which
    reads as data loss.
    """
    if not spec.enumerates_drive:
        return None
    if has_scope(granted, SCOPE_DRIVE_READONLY):
        return None
    return _DRIVE_FILE_NOTICE


def _failure_flags(
    spec: _SectionSpec, exc: Exception, organization_id: int
) -> dict[str, Any]:
    """Explain a failed section without failing the overview.

    Google can still answer "insufficient scope" for a grant this registry
    believed sufficient — a scope revoked one at a time, or an API whose
    requirement changed. That answer is authoritative, so it is reported as a
    scope gap (which the UI can fix by re-consenting) rather than as an outage.
    """
    message = getattr(exc, "message", None) or str(exc)
    missing_scope = getattr(exc, "missing_scope", None)
    code = getattr(exc, "code", None)

    if missing_scope or code == "insufficient_scope":
        logger.info(
            f"Google {spec.name} discovery is out of scope for organization "
            f"{organization_id}: {message}"
        )
        return {
            "available": False,
            "scope_missing": True,
            "missing_scopes": (
                [missing_scope] if missing_scope else list(family_scopes(spec.family))
            ),
            "error": message,
        }

    if isinstance(exc, (GoogleApiError, GoogleOAuthError)):
        logger.warning(
            f"Google {spec.name} discovery failed for organization "
            f"{organization_id}: {message}"
        )
    else:
        # Not a typed integration failure: log the traceback (gather handed the
        # exception back, so there is no live exception context to log from) and
        # tell the customer nothing about our internals.
        logger.opt(exception=exc).error(
            f"Unexpected failure building the Google {spec.name} section for "
            f"organization {organization_id}"
        )
        message = "This section could not be loaded."

    return {
        "available": False,
        "scope_missing": False,
        "missing_scopes": [],
        "error": message,
    }


# ---------------------------------------------------------------------------
# Individual listings (resource pickers)
# ---------------------------------------------------------------------------


async def run_discovery(
    listing: Awaitable[list[ItemT]], *, what: str, organization_id: int
) -> list[ItemT]:
    """Run one listing under the discovery plan's error policy (§11).

    A picker is a browsing surface: "that folder no longer exists" and "Google
    timed out" are empty results with a log line, not error pages. What does
    reach the caller is what the customer can act on — no connection, and a
    grant too narrow for the API — so those propagate as typed errors for the
    route to turn into 400 / 403.
    """
    try:
        return await listing
    except GoogleApiError as exc:
        if exc.missing_scope or exc.code == "insufficient_scope":
            raise
        if exc.http_status == 404 or exc.retryable or exc.code == "network_error":
            logger.warning(
                f"Google {what} discovery returned nothing for organization "
                f"{organization_id}: {exc.message}"
            )
            return []
        raise


async def list_sheet_tabs(
    organization_id: int, spreadsheet_id: str
) -> list[dict[str, Any]]:
    """Tabs of one spreadsheet, shaped for ``SheetTabSummary``."""
    client = await GoogleSheetsClient.for_organization(organization_id)
    meta = await client.get_spreadsheet_meta(spreadsheet_id)
    return [tab for tab in meta.get("sheets") or [] if tab.get("title")]


async def preview_sheet_range(
    organization_id: int, spreadsheet_id: str, a1_range: str | None = None
) -> list[list[Any]]:
    """First cells of a spreadsheet, so the operator can confirm the binding.

    Without an explicit range this previews the first tab: the tab list is
    already fetched to name it, and defaulting to the whole spreadsheet would
    read every tab — including one the operator did not mean to expose.
    """
    client = await GoogleSheetsClient.for_organization(organization_id)

    if not a1_range:
        meta = await client.get_spreadsheet_meta(spreadsheet_id)
        tabs = meta.get("sheets") or []
        if not tabs:
            return []
        a1_range = build_a1_range(tabs[0].get("title"), PREVIEW_DEFAULT_CELLS)

    return await client.read_range(spreadsheet_id, a1_range)
