"""Google Calendar discovery — which calendars exist, and nothing more.

This module lists calendars so an owner can pick one to bind. It deliberately
stops there: it exposes no event listing and no event detail, and that boundary
is a security decision rather than an unfinished feature.

Why the line is drawn here
--------------------------
A bound calendar is reachable from a conversation with an untrusted caller. If
the agent could read events, "what's on your calendar this week?" would read the
owner's private schedule — attendee names, addresses, meeting subjects — out loud
to whoever is on the line, using the owner's own grant. The booking side of the
product answers availability instead (is this slot free?), which is the one bit
of calendar state a caller legitimately needs.

So: discovery lists calendars, and any future runtime capability belongs in the
tool layer with its own gate, not behind a convenient ``list_events`` added here
because the endpoint was one call away.
"""

from __future__ import annotations

from typing import Any, Optional

from api.constants import GOOGLE_CALENDAR_API_BASE_URL, GOOGLE_DISCOVERY_PAGE_SIZE

from .client import GoogleApiClient, GoogleApiError, GoogleScopeError
from .scopes import SCOPE_CALENDAR_READONLY

# Only what CalendarSummary exposes. Google requires the "items(...)" wrapper
# alongside any top-level field such as nextPageToken.
_CALENDAR_FIELDS = (
    "nextPageToken,items(id,summary,summaryOverride,description,primary,"
    "accessRole,colorId,timeZone,deleted,hidden)"
)

# An account with more than a few hundred calendars is a directory, not a
# person's workspace; bounded so a picker cannot become an unbounded crawl.
_MAX_PAGES = 3


class GoogleCalendarError(GoogleApiError):
    """A Calendar call failed."""


class GoogleCalendarScopeError(GoogleCalendarError, GoogleScopeError):
    """A Calendar call was refused for want of a scope."""


class GoogleCalendarClient(GoogleApiClient):
    """Calendar v3 access for one organization's connected Google account."""

    base_url = GOOGLE_CALENDAR_API_BASE_URL
    api_label = "Google Calendar API"
    api_scopes = (SCOPE_CALENDAR_READONLY,)
    error_class = GoogleCalendarError
    scope_error_class = GoogleCalendarScopeError

    async def list_calendar_list(self) -> list[dict[str, Any]]:
        """Every calendar entry in the account's calendar list."""
        page_token: str | None = None
        entries: list[dict[str, Any]] = []

        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "fields": _CALENDAR_FIELDS,
                "maxResults": min(250, GOOGLE_DISCOVERY_PAGE_SIZE),
                # Calendars the owner removed from their list are of no use as a
                # binding target and would only clutter the picker.
                "showDeleted": "false",
                "showHidden": "false",
            }
            if page_token:
                params["pageToken"] = page_token

            payload = await self._request("GET", "/users/me/calendarList", params=params)
            entries.extend(payload.get("items") or [])

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return entries


def _as_calendar_summary(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Narrow one calendarList entry to the summary shape, or drop it."""
    calendar_id = entry.get("id")
    if not calendar_id:
        return None

    return {
        "id": str(calendar_id),
        # summaryOverride is the name the owner gave a calendar someone else
        # shared with them. Preferring it means the picker shows the calendar by
        # the name the owner actually recognises.
        "summary": entry.get("summaryOverride") or entry.get("summary"),
        "description": entry.get("description"),
        "primary": bool(entry.get("primary", False)),
        "access_role": entry.get("accessRole"),
        "color_id": entry.get("colorId"),
        "time_zone": entry.get("timeZone"),
    }


def _sort_key(summary: dict[str, Any]) -> tuple[int, str]:
    """Primary calendar first, then alphabetical.

    The primary calendar is what an owner means by "my calendar" nine times out
    of ten, so it should not be buried in an alphabetical list under a shared
    "Aannouncements" one.
    """
    name = (summary.get("summary") or "").casefold()
    return (0 if summary.get("primary") else 1, name)


async def list_calendars(organization_id: int) -> list[dict[str, Any]]:
    """Calendars the organization's connected account can reach.

    Entries match ``CalendarSummary``. ``access_role`` matters to the caller:
    ``reader`` and ``freeBusyReader`` cannot be booked into, so a UI that offers
    them as booking targets is setting the owner up to fail at runtime.
    """
    client = await GoogleCalendarClient.for_organization(organization_id)
    entries = await client.list_calendar_list()

    summaries = [
        summary
        for summary in (_as_calendar_summary(entry) for entry in entries)
        if summary is not None
    ]
    summaries.sort(key=_sort_key)
    return summaries


__all__ = [
    "GoogleCalendarClient",
    "GoogleCalendarError",
    "GoogleCalendarScopeError",
    "list_calendars",
]
