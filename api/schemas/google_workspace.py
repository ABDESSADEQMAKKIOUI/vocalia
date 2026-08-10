"""Schemas for Google Workspace discovery — what a connected account contains.

These describe *pointers*, never content: a spreadsheet's id and title, a
calendar's name, a Drive file's link. Enough for an operator to pick the
resource an agent will be bound to, and nothing an untrusted conversation could
mine — the one exception is Gmail's ``snippet``, which Google returns as part of
the message list and which the UI shows to the account owner only.

Two conventions carry the whole contract:

- **A section is never just empty.** Every ``WorkspaceSectionResponse`` says
  *why* it has no items: ``scope_missing`` + ``missing_scopes`` when the
  organization's grant does not cover that API, ``error`` when Google failed,
  and neither when the account genuinely has nothing. A silent empty list is the
  bug this whole shape exists to prevent — the customer cannot tell "you never
  granted Calendar" from "you have no calendars".
- **Availability is per organization, not per deployment.** ``granted_scopes``
  is what *this* organization consented to; ``requested_scopes`` is what the
  deployment asks for at all. A scope in neither list cannot be fixed by
  re-consenting — the operator has to widen ``GOOGLE_OAUTH_SCOPES`` first (see
  ``services/integrations/google/scopes.py`` for what that costs).

Field names are snake_case, matching ``api/schemas/google_sheets.py`` and the
rest of the repo; the TypeScript client is generated from the OpenAPI these
models produce, so every field carries a description.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

ItemT = TypeVar("ItemT")


# ---------------------------------------------------------------------------
# Resource summaries
# ---------------------------------------------------------------------------


class SpreadsheetSummary(BaseModel):
    """One Google Sheets file the account can reach."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description=(
            "Drive file id, which is also the spreadsheetId the Sheets API takes."
        )
    )
    name: str = Field(description="Spreadsheet title as it reads in Drive.")
    web_url: Optional[str] = Field(
        default=None,
        description="Link that opens the spreadsheet in Google Sheets.",
    )
    modified_time: Optional[datetime] = Field(
        default=None, description="When the file was last modified."
    )


class SheetTabSummary(BaseModel):
    """One tab (worksheet) inside a spreadsheet."""

    model_config = ConfigDict(from_attributes=True)

    sheet_id: Optional[int] = Field(
        default=None,
        description=(
            "Numeric tab id assigned by Google. Stable across renames, unlike "
            "the title — but the title is what A1 ranges use."
        ),
    )
    title: str = Field(description="Tab name, e.g. 'Leads 2026'.")
    index: Optional[int] = Field(
        default=None, description="0-based position of the tab in the spreadsheet."
    )
    row_count: Optional[int] = Field(
        default=None, description="Grid height, including empty rows."
    )
    column_count: Optional[int] = Field(
        default=None, description="Grid width, including empty columns."
    )


class CalendarSummary(BaseModel):
    """One calendar in the account's calendar list."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(
        description=(
            "Calendar id used in Calendar API calls; 'primary' for the main one."
        )
    )
    summary: Optional[str] = Field(
        default=None, description="Human-readable calendar name."
    )
    description: Optional[str] = Field(
        default=None, description="Calendar description, when the owner set one."
    )
    primary: bool = Field(
        default=False, description="True for the account's primary calendar."
    )
    access_role: Optional[str] = Field(
        default=None,
        description=(
            "What the connected account may do here: 'owner', 'writer', "
            "'reader' or 'freeBusyReader'. Anything below 'writer' cannot host "
            "bookings."
        ),
    )
    color_id: Optional[str] = Field(
        default=None, description="Google's palette id, so the UI can match colours."
    )
    time_zone: Optional[str] = Field(
        default=None,
        description=(
            "IANA time zone of the calendar. Booking times are meaningless "
            "without it."
        ),
    )


class DriveItemSummary(BaseModel):
    """One Drive file or folder."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Drive file or folder id.")
    name: str = Field(description="File or folder name.")
    mime_type: Optional[str] = Field(
        default=None,
        description=(
            "Google MIME type. 'application/vnd.google-apps.folder' marks a "
            "folder; see also `is_folder`."
        ),
    )
    is_folder: bool = Field(
        default=False,
        description=(
            "Convenience flag so the UI does not have to string-match the MIME "
            "type to decide whether an item can be opened."
        ),
    )
    web_url: Optional[str] = Field(
        default=None, description="Link that opens the item in Google Drive."
    )
    modified_time: Optional[datetime] = Field(
        default=None, description="When the item was last modified."
    )


class GmailLabelSummary(BaseModel):
    """One Gmail label."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Label id used in Gmail API calls, e.g. 'INBOX'.")
    name: str = Field(description="Label name shown in Gmail.")
    type: Optional[str] = Field(
        default=None,
        description="'system' for Google's built-in labels, 'user' for custom ones.",
    )


class GmailMessageSummary(BaseModel):
    """Headers and snippet of one message — never its body."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Message id.")
    thread_id: Optional[str] = Field(
        default=None, description="Thread the message belongs to."
    )
    subject: Optional[str] = Field(default=None, description="Subject header.")
    from_address: Optional[str] = Field(
        default=None,
        description=(
            "The From header, verbatim (display name and address). Named "
            "`from_address` because `from` is a Python keyword."
        ),
    )
    date: Optional[str] = Field(
        default=None,
        description=(
            "The Date header as Gmail returns it. Kept as text on purpose: "
            "senders emit malformed dates and a parse failure must not drop the "
            "message from the list."
        ),
    )
    snippet: Optional[str] = Field(
        default=None,
        description=(
            "Short preview Google includes in the message list. Shown to the "
            "account owner only."
        ),
    )


class DocsSummary(BaseModel):
    """One Google Docs document."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Drive file id, which is the documentId for Docs.")
    name: str = Field(description="Document title.")
    web_url: Optional[str] = Field(
        default=None, description="Link that opens the document in Google Docs."
    )
    modified_time: Optional[datetime] = Field(
        default=None, description="When the document was last modified."
    )


# ---------------------------------------------------------------------------
# Workspace overview
# ---------------------------------------------------------------------------


class WorkspaceSectionFlags(BaseModel):
    """Why a section looks the way it does.

    Read the flags before the items: an empty section with ``scope_missing`` is
    a permission to ask for, an empty section with ``error`` is Google failing,
    and an empty section with neither is an account that has nothing there.
    """

    model_config = ConfigDict(from_attributes=True)

    available: bool = Field(
        default=False,
        description=(
            "True when this organization's grant covers the section's API and "
            "the listing succeeded. False means the items are empty for a "
            "reason given by the other fields."
        ),
    )
    scope_missing: bool = Field(
        default=False,
        description=(
            "True when the connected account never granted the scopes this "
            "section needs."
        ),
    )
    missing_scopes: List[str] = Field(
        default_factory=list,
        description=(
            "Exact scopes to add to the consent to make this section work. "
            "Empty when nothing is missing."
        ),
    )
    error: Optional[str] = Field(
        default=None,
        description=(
            "Set when the scopes were granted but the call to Google failed. "
            "One section failing never fails the whole overview."
        ),
    )
    notice: Optional[str] = Field(
        default=None,
        description=(
            "Set when the section worked but the grant narrows what it can "
            "see, so a short or empty list is expected rather than wrong. The "
            "case in practice is drive.file, which enumerates only the files "
            "this application was given — an account with a thousand "
            "spreadsheets legitimately shows none. Without this the flags say "
            "'available, no error, no items' and the owner concludes their "
            "files vanished."
        ),
    )


class WorkspaceSectionResponse(WorkspaceSectionFlags, Generic[ItemT]):
    """A section that is simply a list of one resource type."""

    items: List[ItemT] = Field(
        default_factory=list, description="Resources found, newest first."
    )


class WorkspaceDriveSection(WorkspaceSectionFlags):
    """Drive is shown as two lists: where to browse, and what changed lately."""

    recent_files: List[DriveItemSummary] = Field(
        default_factory=list,
        description="Most recently modified files, folders excluded.",
    )
    root_folders: List[DriveItemSummary] = Field(
        default_factory=list,
        description="Top-level folders of My Drive, as an entry point for browsing.",
    )


class WorkspaceGmailSection(WorkspaceSectionFlags):
    """Gmail is shown as its labels plus a short peek at the inbox."""

    labels: List[GmailLabelSummary] = Field(
        default_factory=list, description="Labels available for inbox automation."
    )
    recent: List[GmailMessageSummary] = Field(
        default_factory=list,
        description="A handful of the most recent inbox messages, headers only.",
    )


class WorkspaceOverviewResponse(BaseModel):
    """Everything a connected Google account exposes, in one call.

    Answers the question the UI asks right after the consent redirect — "what
    did I just connect, and what can it do?" — without five round-trips whose
    empty results cannot be told apart.
    """

    model_config = ConfigDict(from_attributes=True)

    connected: bool = Field(
        description=(
            "True when the organization has a usable, non-revoked Google "
            "connection. Every section is empty when this is false."
        )
    )
    google_email: Optional[str] = Field(
        default=None,
        description=(
            "Google account behind the connection, when known. Stays null when "
            "no identity scope was granted."
        ),
    )
    status: Optional[str] = Field(
        default=None,
        description=(
            "'connected' or 'revoked'; null when the organization never connected."
        ),
    )
    requires_reconsent: bool = Field(
        default=False,
        description=(
            "True when the grant is gone and the user must go through the "
            "consent screen again — refreshing cannot recover it."
        ),
    )
    granted_scopes: List[str] = Field(
        default_factory=list,
        description="Scopes this organization's consent actually covers.",
    )
    requested_scopes: List[str] = Field(
        default_factory=list,
        description=(
            "Scopes this deployment asks for at all (GOOGLE_OAUTH_SCOPES). A "
            "scope missing from here cannot be granted by re-consenting."
        ),
    )
    sheets: WorkspaceSectionResponse[SpreadsheetSummary] = Field(
        default_factory=WorkspaceSectionResponse[SpreadsheetSummary],
        description="Spreadsheets the account can reach.",
    )
    calendars: WorkspaceSectionResponse[CalendarSummary] = Field(
        default_factory=WorkspaceSectionResponse[CalendarSummary],
        description="Calendars in the account's calendar list.",
    )
    drive: WorkspaceDriveSection = Field(
        default_factory=WorkspaceDriveSection,
        description="Drive entry points: top-level folders and recent files.",
    )
    gmail: WorkspaceGmailSection = Field(
        default_factory=WorkspaceGmailSection,
        description="Gmail labels and a short inbox preview.",
    )
    docs: WorkspaceSectionResponse[DocsSummary] = Field(
        default_factory=WorkspaceSectionResponse[DocsSummary],
        description="Google Docs documents the account can reach.",
    )


# ---------------------------------------------------------------------------
# Scope registry
# ---------------------------------------------------------------------------


class GoogleScopeInfo(BaseModel):
    """One scope, with what it costs and who holds it."""

    model_config = ConfigDict(from_attributes=True)

    scope: str = Field(description="Full scope URL as Google spells it.")
    sensitivity: str = Field(
        description=(
            "Google's classification: 'non_sensitive' (nothing to do), "
            "'sensitive' (app verification), 'restricted' (verification plus a "
            "paid annual security assessment), or 'unknown' for a scope this "
            "deployment configured that the registry does not classify."
        )
    )
    requested: bool = Field(
        description="Whether this deployment's consent screen asks for the scope."
    )
    granted: bool = Field(
        default=False,
        description=(
            "Whether the calling organization's grant covers it, counting "
            "broader scopes that imply it."
        ),
    )


class GoogleRequiredScopesResponse(BaseModel):
    """The server's answer to 'which scopes does this tool need?'.

    The browser must not hardcode scope lists: which scopes a family needs is a
    server decision, and which ones this deployment may legally request is an
    operator decision. Both travel here.
    """

    model_config = ConfigDict(from_attributes=True)

    families: Dict[str, List[str]] = Field(
        description=(
            "Scopes per tool family — 'sheets', 'calendar', 'drive', 'gmail', "
            "'docs', plus 'all' for the union."
        )
    )
    requested_scopes: List[str] = Field(
        default_factory=list,
        description=(
            "What this deployment actually sends to Google's consent screen. "
            "Families listing a scope absent from here are not usable until an "
            "operator widens GOOGLE_OAUTH_SCOPES."
        ),
    )
    deployment_sensitivity: str = Field(
        default="non_sensitive",
        description=(
            "Heaviest classification among the requested scopes — what review "
            "this deployment owes Google as configured."
        ),
    )
    scope_details: List[GoogleScopeInfo] = Field(
        default_factory=list,
        description="Per-scope classification, so the UI can warn before widening.",
    )
