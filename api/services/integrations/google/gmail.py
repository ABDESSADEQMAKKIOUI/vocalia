"""Gmail discovery — labels, and a metadata-only peek at recent messages.

``gmail.readonly`` is a RESTRICTED scope: on top of Google's verification it
requires the annual, paid CASA security assessment. This deployment does not
request it by default (see :mod:`.scopes`), so on a stock install this section
of the workspace overview reports itself unavailable rather than empty — which
is the whole point of the section flags.

Listing messages costs two round trips per message
--------------------------------------------------
``users.messages.list`` returns only ``{id, threadId}``: no subject, no sender,
no date. Every one of those has to come from a second ``users.messages.get``.
That shape drives two decisions here:

* **format=metadata with an explicit header allow-list.** ``format=full`` would
  return the entire message body, including attachments' structure. Pulling a
  mailbox's contents through this application to render a five-line preview is
  a data-exposure decision nobody asked for, so only Subject, From and Date come
  back. The snippet Gmail attaches is a short excerpt Gmail itself computes and
  shows in its own list view.
* **Bounded concurrency.** The detail fetches run together, or a 25-message
  preview costs 25 sequential round trips. But unbounded ``gather`` on 50
  messages fires 50 simultaneous requests at a per-user quota and earns a 429
  for the whole batch, so a semaphore caps how many are in flight at once.

A single message that fails to load is dropped rather than failing the list: one
inaccessible message should not blank the preview.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from loguru import logger

from api.constants import GOOGLE_GMAIL_API_BASE_URL

from .client import GoogleApiClient, GoogleApiError, GoogleScopeError
from .scopes import SCOPE_GMAIL_READONLY

# Only the headers the summary shows. Anything else is data this application has
# no reason to hold.
_METADATA_HEADERS = ("Subject", "From", "Date")

# Enough to keep the preview quick without crowding the per-user rate limit.
_DETAIL_CONCURRENCY = 5

# A preview, not an inbox client. Guards against a caller asking for thousands.
_MAX_RESULTS_CEILING = 50


class GoogleGmailError(GoogleApiError):
    """A Gmail call failed."""


class GoogleGmailScopeError(GoogleGmailError, GoogleScopeError):
    """A Gmail call was refused for want of a scope."""


class GoogleGmailClient(GoogleApiClient):
    """Gmail v1 access for one organization's connected Google account."""

    base_url = GOOGLE_GMAIL_API_BASE_URL
    api_label = "Gmail API"
    api_scopes = (SCOPE_GMAIL_READONLY,)
    error_class = GoogleGmailError
    scope_error_class = GoogleGmailScopeError

    async def fetch_labels(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/users/me/labels")
        return payload.get("labels") or []

    async def fetch_message_ids(
        self, *, label_id: Optional[str], max_results: int
    ) -> list[str]:
        params: dict[str, Any] = {"maxResults": max_results}
        if label_id:
            params["labelIds"] = label_id

        payload = await self._request("GET", "/users/me/messages", params=params)
        return [
            str(entry["id"])
            for entry in payload.get("messages") or []
            if entry.get("id")
        ]

    async def fetch_message_metadata(self, message_id: str) -> Optional[dict[str, Any]]:
        """One message's headers and snippet, or ``None`` if it cannot be read."""
        try:
            return await self._request(
                "GET",
                f"/users/me/messages/{message_id}",
                params={
                    "format": "metadata",
                    "metadataHeaders": list(_METADATA_HEADERS),
                },
            )
        except GoogleGmailScopeError:
            # A scope gap is not about this one message — it applies to all of
            # them, and the caller has to hear about it.
            raise
        except GoogleApiError as exc:
            logger.warning(f"Skipping Gmail message {message_id}: {exc}")
            return None


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    """Index a message's headers by lower-cased name.

    Header names are case-insensitive per RFC 5322, and Gmail does not promise a
    particular casing, so matching on the exact string Google happened to return
    is how a subject silently goes missing.
    """
    headers = (payload.get("payload") or {}).get("headers") or []
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in headers
        if header.get("name")
    }


def _as_message_summary(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Narrow one message to the summary shape, or drop it."""
    message_id = payload.get("id")
    if not message_id:
        return None

    headers = _header_map(payload)
    return {
        "id": str(message_id),
        "thread_id": payload.get("threadId"),
        "subject": headers.get("subject") or None,
        # "from" is a Python keyword, so the schema names this from_address.
        "from_address": headers.get("from") or None,
        # Left as Google's raw RFC 2822 string rather than parsed: a malformed
        # or exotic date in one message should not fail the whole preview, and
        # the frontend is what decides how to display it.
        "date": headers.get("date") or None,
        "snippet": payload.get("snippet") or None,
    }


async def list_labels(organization_id: int) -> list[dict[str, Any]]:
    """Gmail labels, system ones first then user labels alphabetically.

    Entries match ``GmailLabelSummary``. System labels (INBOX, SENT…) lead
    because they are what a filter is usually built on.
    """
    client = await GoogleGmailClient.for_organization(organization_id)
    labels = await client.fetch_labels()

    summaries = [
        {
            "id": str(label["id"]),
            "name": label.get("name") or str(label["id"]),
            "type": label.get("type"),
        }
        for label in labels
        if label.get("id")
    ]
    summaries.sort(
        key=lambda item: (
            0 if item.get("type") == "system" else 1,
            (item.get("name") or "").casefold(),
        )
    )
    return summaries


async def list_messages(
    organization_id: int,
    label_id: Optional[str] = "INBOX",
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Recent messages under one label, as metadata-only summaries.

    Order follows Gmail's own (most recent first) and is preserved through the
    concurrent detail fetches — ``gather`` returns results positionally, so the
    preview does not come back shuffled.
    """
    limit = max(1, min(int(max_results or 10), _MAX_RESULTS_CEILING))

    client = await GoogleGmailClient.for_organization(organization_id)
    message_ids = await client.fetch_message_ids(label_id=label_id, max_results=limit)
    if not message_ids:
        return []

    semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)

    async def _load(message_id: str) -> Optional[dict[str, Any]]:
        async with semaphore:
            return await client.fetch_message_metadata(message_id)

    payloads = await asyncio.gather(*(_load(mid) for mid in message_ids))

    summaries = [
        summary
        for summary in (
            _as_message_summary(payload) for payload in payloads if payload
        )
        if summary is not None
    ]
    logger.debug(
        f"Loaded {len(summaries)}/{len(message_ids)} Gmail message summaries for "
        f"organization {organization_id} (label={label_id})"
    )
    return summaries


__all__ = [
    "GoogleGmailClient",
    "GoogleGmailError",
    "GoogleGmailScopeError",
    "list_labels",
    "list_messages",
]
