"""Schemas for the Google connection and Google Sheets tool binding.

Request/response bodies for ``api/routes/integrations_google.py``.

Two rules shape everything here:

- **No secret ever leaves the backend.** The connection response carries state
  (connected, which account, when the token expires) and never a token value.
  ``GoogleOAuthPublicConfig`` is browser-safe by construction — the OAuth client
  id and the Picker keys are public values; the client secret has no field.
- **The inferred schema is shown, not hidden.** Binding a sheet returns every
  column the inference understood, with the exact description the agent's LLM
  will be given and the column letter the server will write to. That is the
  surface the UI renders so the customer can confirm the system read their sheet
  correctly. An inference nobody sees is an inference nobody can correct.

Response models use ``from_attributes`` so the binding service can return its own
dataclasses without the routes hand-copying every field.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# A Drive file id: what the Google Picker hands back. Kept deliberately strict —
# this value is interpolated into Sheets API paths.
_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
# ...and the same id as it appears in a spreadsheet URL, so a pasted browser
# address is accepted instead of failing with an unhelpful format error.
_SPREADSHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{10,200})")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class GoogleOAuthPublicConfig(BaseModel):
    """Browser-safe deployment config for the connect button and the Picker.

    The Picker is not optional: the ``drive.file`` scope only grants access to
    files the user explicitly picks, so a hand-typed spreadsheet id comes back
    404 from Google (see ``services/integrations/google/oauth.py``).
    """

    model_config = ConfigDict(from_attributes=True)

    enabled: bool = Field(
        description="Whether this deployment has a Google OAuth client configured."
    )
    client_id: Optional[str] = None
    picker_api_key: Optional[str] = None
    picker_app_id: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)


class GoogleAuthorizeResponse(BaseModel):
    """Where to send the browser to start the consent round-trip."""

    authorization_url: str = Field(
        description="Google consent URL carrying a signed, organization-bound state."
    )
    redirect_uri: str = Field(
        description=(
            "Redirect URI embedded in the authorization URL. Must be registered "
            "verbatim on the deployment's Google OAuth client."
        )
    )
    expires_in_seconds: int = Field(
        description=(
            "How long the authorization URL stays usable before its state expires."
        )
    )


class GoogleConnectionStatusResponse(BaseModel):
    """The organization's Google connection, without any credential material."""

    connected: bool = Field(
        description="True when the organization has a usable (non-revoked) connection."
    )
    status: Optional[str] = Field(
        default=None,
        description=(
            "'connected' or 'revoked'. Null when the organization never connected."
        ),
    )
    account_email: Optional[str] = Field(
        default=None,
        description=(
            "Google account behind the connection, when known. The granted scopes "
            "do not include an identity scope, so this stays null unless a later "
            "call resolved it."
        ),
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Access token expiry. Not an expiry of the connection itself — the "
            "backend refreshes it silently."
        ),
    )
    scopes: List[str] = Field(default_factory=list)
    requires_reconsent: bool = Field(
        default=False,
        description=(
            "True when Google revoked the grant: refreshing cannot recover it and "
            "the user must go through the consent screen again."
        ),
    )
    revoked_reason: Optional[str] = None
    oauth_config: GoogleOAuthPublicConfig = Field(
        description=(
            "Deployment-level public config the UI needs to connect and pick a file."
        )
    )


class GoogleDisconnectResponse(BaseModel):
    """Result of revoking and deleting an organization's Google connection."""

    disconnected: bool = Field(
        description="False when the organization had nothing to disconnect."
    )


# ---------------------------------------------------------------------------
# Sheet binding
# ---------------------------------------------------------------------------


class SheetBindRequest(BaseModel):
    """Bind one tab of one spreadsheet to an agent tool.

    No column is declared here on purpose: the whole point of the feature is
    that the header row is read and understood by the LLM once, at bind time.
    """

    spreadsheet_id: str = Field(
        description=(
            "Drive file id of the spreadsheet, as returned by the Google Picker. "
            "A full spreadsheet URL is accepted and reduced to its id."
        )
    )
    sheet_name: str = Field(
        min_length=1,
        max_length=255,
        description="Tab name inside the spreadsheet, e.g. 'Leads 2026'.",
    )
    tool_name: str = Field(
        min_length=1,
        max_length=255,
        description="Display name of the tool created (or updated) for this sheet.",
    )
    header_row: int = Field(
        default=1,
        ge=1,
        le=100,
        description="1-based row holding the column headers.",
    )
    tool_uuid: Optional[str] = Field(
        default=None,
        description=(
            "Existing google_sheets tool to re-point at this sheet. Omit to let the "
            "backend create one (or reuse the tool already bound to this tab)."
        ),
    )
    description: Optional[str] = Field(
        default=None,
        description=(
            "Optional description shown to the agent when it decides whether to "
            "call the tool. Generated from the sheet when omitted."
        ),
    )

    @field_validator("spreadsheet_id", mode="before")
    @classmethod
    def normalize_spreadsheet_id(cls, v: Any) -> str:
        """Accept either a bare file id or a pasted spreadsheet URL."""
        raw = str(v or "").strip()
        url_match = _SPREADSHEET_URL_RE.search(raw)
        if url_match:
            return url_match.group(1)
        if not _SPREADSHEET_ID_RE.match(raw):
            raise ValueError(
                "spreadsheet_id must be a Google Drive file id or a spreadsheet URL"
            )
        return raw

    @field_validator("sheet_name", "tool_name")
    @classmethod
    def strip_required_text(cls, v: str) -> str:
        stripped = str(v).strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped


class SheetSchemaRefreshRequest(BaseModel):
    """Body for the refresh endpoint. Optional — an empty body means force=False."""

    force: bool = Field(
        default=False,
        description=(
            "Re-run the inference even when the header row is unchanged. Off by "
            "default: re-inferring on every call costs an LLM round-trip and makes "
            "the agent's parameters drift between calls."
        ),
    )


class SheetColumnSchema(BaseModel):
    """One understood column of a bound sheet.

    ``parameter_name`` is what the agent's LLM sees as a named function
    parameter; ``column_letter`` is what the server writes to. The model never
    sees the letter and never chooses a position — it fills a named, described
    parameter and the server resolves placement.
    """

    model_config = ConfigDict(from_attributes=True)

    header: str = Field(description="Exact header text as it appears in the sheet.")
    parameter_name: str = Field(
        description="Parameter name exposed to the agent for this column."
    )
    column_letter: str = Field(
        description="A1 column the server writes this value to, e.g. 'C'."
    )
    type: str = Field(description="Inferred JSON type: string, number, or boolean.")
    description: str = Field(
        description="Inferred meaning of the column, shown to the agent verbatim."
    )
    required: bool = Field(
        default=False,
        description="Whether the agent must provide a value for this column.",
    )
    examples: List[str] = Field(
        default_factory=list,
        description="Sample values read from the sheet, used to ground the inference.",
    )


class SheetBindResponse(BaseModel):
    """The bound sheet and everything the inference understood about it."""

    model_config = ConfigDict(from_attributes=True)

    tool_uuid: str
    tool_name: str
    created: bool = Field(
        description=(
            "True when a new tool was created, False when an existing one was updated."
        )
    )
    spreadsheet_id: str
    spreadsheet_title: Optional[str] = None
    sheet_name: str
    header_row: int
    header_fingerprint: str = Field(
        description=(
            "Fingerprint of the header row the schema was inferred from. The "
            "inference is only redone when this changes."
        )
    )
    inferred_at: Optional[datetime] = None
    columns: List[SheetColumnSchema] = Field(
        default_factory=list,
        description=(
            "Understood columns, in sheet order. The UI shows these for confirmation."
        ),
    )
    warnings: List[str] = Field(
        default_factory=list,
        description=(
            "Things the customer should look at — blank headers, duplicate header "
            "names, columns the inference was unsure about."
        ),
    )


class SheetSchemaRefreshResponse(BaseModel):
    """Outcome of re-checking a bound sheet's header row."""

    model_config = ConfigDict(from_attributes=True)

    tool_uuid: str
    refreshed: bool = Field(
        description=(
            "False when the header row was unchanged and the cached schema was kept."
        )
    )
    header_fingerprint: str
    previous_fingerprint: Optional[str] = None
    inferred_at: Optional[datetime] = None
    columns: List[SheetColumnSchema] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class SheetPreviewResponse(BaseModel):
    """Live headers and first rows next to the cached schema.

    ``schema_stale`` is the actionable field: it is True when the sheet's header
    row no longer matches the fingerprint the cached schema was inferred from,
    which is exactly when the UI should offer to refresh.
    """

    model_config = ConfigDict(from_attributes=True)

    tool_uuid: str
    spreadsheet_id: str
    spreadsheet_title: Optional[str] = None
    sheet_name: str
    header_row: int
    headers: List[str] = Field(
        default_factory=list,
        description="Header row as it reads in the sheet right now.",
    )
    column_letters: List[str] = Field(
        default_factory=list,
        description="Column letters, positionally aligned with `headers`.",
    )
    rows: List[List[Any]] = Field(
        default_factory=list,
        description=(
            "First data rows. Google trims trailing empty cells, so rows can be "
            "shorter than `headers`."
        ),
    )
    columns: List[SheetColumnSchema] = Field(
        default_factory=list, description="Cached inferred schema for this tool."
    )
    header_fingerprint: Optional[str] = Field(
        default=None, description="Fingerprint the cached schema was inferred from."
    )
    schema_stale: bool = Field(
        default=False,
        description=(
            "True when the live header row no longer matches the cached schema."
        ),
    )
