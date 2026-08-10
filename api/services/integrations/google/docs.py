"""Google Docs discovery for the workspace overview (plan §2.1, §3.1).

Listing documents is a **Drive** query, not a Docs one
-----------------------------------------------------
The Docs API (``docs.googleapis.com/v1/documents/{id}``) only reads one document
whose id you already have — it has no list endpoint. Enumerating "the documents
this account can open" is a Drive files query filtered on the Docs mime type, so
this module delegates to :mod:`.drive` instead of duplicating the query, its
paging, and its field mask. What stays here is what is specific to Docs: the
summary shape the API returns (:class:`api.schemas.google_workspace.DocsSummary`)
and the ``docs.google.com`` link, which Drive only sends when the file's
``webViewLink`` field was granted — the id alone is enough to rebuild it.

Because the underlying call is Drive's, a too-narrow grant surfaces as Drive's
scope error (``drive.readonly``), which is the scope the caller actually has to
add; the ``documents`` scope only matters once a document is read.
"""

from __future__ import annotations

from typing import Any

from . import drive

# Google Docs files carry this mime type in Drive; kept here as the Docs-side
# name for it so callers do not have to reach into Drive for the constant.
DOCUMENT_MIME_TYPE = "application/vnd.google-apps.document"

_DOCUMENT_URL_TEMPLATE = "https://docs.google.com/document/d/{document_id}/edit"


def document_url(document_id: str) -> str:
    """Link that opens a document in the Google Docs editor."""
    return _DOCUMENT_URL_TEMPLATE.format(document_id=document_id)


async def list_documents(organization_id: int) -> list[dict[str, Any]]:
    """Google Docs documents the organization's connected account can open.

    Each entry matches ``DocsSummary``: ``id``, ``name``, ``web_url``,
    ``modified_time``. Raises the Drive client's typed errors (scope gaps
    included) — the caller decides whether an empty or failed section is a
    missing scope or a genuinely empty Drive.
    """
    files = await drive.list_documents(organization_id)
    return [_as_document_summary(item) for item in files if item.get("id")]


def _as_document_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Narrow one Drive file to the Docs summary, filling in a missing link."""
    document_id = str(item["id"])
    return {
        "id": document_id,
        # Drive returns "Untitled document" itself, but a file listed without a
        # name would otherwise fail DocsSummary validation on a required field.
        "name": item.get("name") or "Untitled document",
        "web_url": item.get("web_url") or document_url(document_id),
        "modified_time": item.get("modified_time"),
    }
