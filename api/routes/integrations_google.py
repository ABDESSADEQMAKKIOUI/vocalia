"""Google connection, workspace discovery, and Google Sheets binding endpoints.

Connecting is a three-legged OAuth round-trip that leaves the browser, so the
callback cannot be authenticated the usual way: Google redirects the user back
here with no session and no header. Which organization consented therefore comes
from the signed ``state`` minted by ``/authorize`` — never from a query
parameter. That binding is the security of this router: without it, anyone could
finish a consent round-trip against another organization's callback and attach
THEIR Google account to it, which on the write path means an agent quietly
appending another tenant's call data into an attacker's spreadsheet.

Binding a sheet does the opposite of the old prototype, which froze twelve
column names inside the tool's description text (reword the prompt, break the
schema, silently). Here the header row is read once, the LLM is asked what each
column means, and the result is cached against a fingerprint of that header row
— re-inferred only when the headers actually change. Inferring per call would
cost an LLM round-trip every conversation turn and give a different answer each
time.

Every response returns the understood columns with their inferred descriptions.
That is deliberate: the customer has to be able to see, and correct, what the
system believes their sheet means.

The discovery endpoints (``/workspace/overview`` and ``/tools/...``) answer the
other half of the story: what the connected account actually contains, so an
operator picks a resource from a list instead of pasting an id. They read
Google with the *organization's own* token — no id in a request grants anything,
it only names something that token may or may not open. The single exception is
``/oauth/required-scopes``, which is public because it returns this deployment's
static scope registry and reads no tenant at all.

Route handlers stay thin — they resolve the caller's ``organization_id``,
validate ownership of the referenced tool, delegate to
``services/integrations/google/``, and translate typed integration errors into
HTTP status codes.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable
from typing import Any, Optional, TypeVar
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from loguru import logger

from api.constants import (
    GOOGLE_DISCOVERY_RECENT_MESSAGES,
    GOOGLE_OAUTH_REDIRECT_URI,
    GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS,
    UI_APP_URL,
)
from api.db import db_client
from api.db.models import ToolModel, UserModel
from api.schemas.google_sheets import (
    GoogleAuthorizeResponse,
    GoogleConnectionStatusResponse,
    GoogleDisconnectResponse,
    GoogleOAuthPublicConfig,
    SheetBindRequest,
    SheetBindResponse,
    SheetPreviewResponse,
    SheetSchemaRefreshRequest,
    SheetSchemaRefreshResponse,
)
from api.schemas.google_workspace import (
    CalendarSummary,
    DocsSummary,
    DriveItemSummary,
    GmailLabelSummary,
    GmailMessageSummary,
    GoogleRequiredScopesResponse,
    GoogleScopeInfo,
    SheetTabSummary,
    SpreadsheetSummary,
    WorkspaceOverviewResponse,
)
from api.services.auth.depends import get_user_with_selected_organization
from api.services.integrations.google import calendar as google_calendar
from api.services.integrations.google import docs as google_docs
from api.services.integrations.google import drive as google_drive
from api.services.integrations.google import gmail as google_gmail
from api.services.integrations.google import scopes as google_scopes
from api.services.integrations.google import workspace as google_workspace
from api.services.integrations.google.client import GoogleApiError
from api.services.integrations.google.oauth import (
    GoogleOAuthError,
    build_authorization_url,
    disconnect,
    exchange_code,
    get_connection,
    google_oauth_public_config,
    is_google_oauth_configured,
    parse_oauth_state,
    store_credentials,
)
from api.services.integrations.google.sheets_binding import (
    GOOGLE_SHEETS_TOOL_TYPE,
    bind_sheet,
    preview_sheet,
    refresh_sheet_schema,
)
from api.services.integrations.google.sheets_client import GoogleSheetsError

router = APIRouter(prefix="/integrations/google", tags=["integrations-google"])

# Page the UI hosts to show the outcome of the consent round-trip. The callback
# can only redirect the browser somewhere — it has no response the UI can read.
UI_RETURN_PATH = "/integrations/google"

# Anything outside this set is squashed before an error code is handed back to
# the UI (see _ui_redirect).
_ERROR_CODE_RE = re.compile(r"[^a-z0-9_.-]+")


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def _google_http_error(exc: GoogleOAuthError | GoogleApiError) -> HTTPException:
    """Map a typed integration error onto an HTTP response.

    The detail is a dict, not a string, because the UI has to branch on the
    outcome: "reconnect your Google account" and "that spreadsheet does not
    exist" need different buttons, and the message alone cannot be parsed.
    """
    requires_reconsent = bool(getattr(exc, "requires_reconsent", False))
    code = getattr(exc, "code", "google_error")

    if code == "not_configured":
        # Deployment-level: no OAuth client is configured at all.
        status_code = 503
    elif requires_reconsent:
        # 409, not 401: the caller is authenticated with Volira just fine — it is
        # the organization's Google grant that is gone.
        status_code = 409
    elif code in ("invalid_state", "expired_state"):
        status_code = 400
    else:
        upstream_status = getattr(exc, "http_status", None)
        if upstream_status in (400, 403, 404):
            status_code = upstream_status
        else:
            # Anything else is Google failing or unreachable, not the caller.
            status_code = 502

    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": getattr(exc, "message", str(exc)),
            "requires_reconsent": requires_reconsent,
        },
    )


def _discovery_http_error(exc: GoogleOAuthError | GoogleApiError) -> HTTPException:
    """Map a discovery failure onto the codes the discovery plan pins (§11).

    Two of them differ from the binding routes above and are what the picker UI
    branches on:

    - **No connection is a 400, not the 409** ``_google_http_error`` returns.
      The organization never connected, so the answer is "connect Google",
      not "your connection expired, reconnect it".
    - **A too-narrow grant is a 403 naming the scope.** The grant is valid; it
      just does not reach this API, and the UI has to be able to ask for that
      one permission rather than restart the whole consent.
    """
    missing_scope = getattr(exc, "missing_scope", None)
    code = getattr(exc, "code", "google_error")

    if isinstance(exc, GoogleOAuthError) and exc.requires_reconsent:
        return HTTPException(
            status_code=400,
            detail={
                "code": code,
                "message": "Google account not connected",
                "requires_reconsent": True,
            },
        )

    if missing_scope or code == "insufficient_scope":
        return HTTPException(
            status_code=403,
            detail={
                "code": "insufficient_scope",
                "message": getattr(exc, "message", str(exc)),
                "missing_scope": missing_scope,
                "requires_reconsent": False,
            },
        )

    return _google_http_error(exc)


ItemT = TypeVar("ItemT")


async def _discover(
    listing: Awaitable[list[ItemT]], *, what: str, organization_id: int
) -> list[ItemT]:
    """Await one discovery listing and translate its typed failures."""
    try:
        return await google_workspace.run_discovery(
            listing, what=what, organization_id=organization_id
        )
    except (GoogleOAuthError, GoogleApiError) as exc:
        raise _discovery_http_error(exc) from exc


async def _resolve_sheet_tool(tool_uuid: str, organization_id: int) -> ToolModel:
    """Load a google_sheets tool that belongs to this organization.

    Ownership is checked here rather than in the service: the uuid comes from the
    URL, and a uuid in a request never implies the caller may touch the row
    behind it. A tool from another organization is reported as missing.
    """
    tool = await db_client.get_tool_by_uuid(tool_uuid, organization_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    definition = tool.definition or {}
    if definition.get("type") != GOOGLE_SHEETS_TOOL_TYPE:
        raise HTTPException(
            status_code=400,
            detail=f"Tool {tool_uuid} is not a {GOOGLE_SHEETS_TOOL_TYPE} tool",
        )
    return tool


def _ui_redirect(*, status: str, reason: Optional[str] = None) -> RedirectResponse:
    """Send the browser back to the UI with the outcome of the consent flow.

    Only an outcome and an error code travel in the query string — never an
    account, a token, or anything else about the user. The reason is reduced to a
    short code: part of it comes from whoever called the callback, and the UI
    displays it, so free text has no business reaching the page.
    """
    params: dict[str, str] = {"google_status": status}
    if reason:
        code = _ERROR_CODE_RE.sub("_", str(reason).strip().lower())[:64]
        params["reason"] = code or "unknown_error"
    url = f"{UI_APP_URL.rstrip('/')}{UI_RETURN_PATH}?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=302)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


@router.get("/oauth/required-scopes", response_model=GoogleRequiredScopesResponse)
async def get_google_required_scopes() -> GoogleRequiredScopesResponse:
    """Which Google scopes each tool family needs, and which this deployment asks for.

    Public on purpose, and safe to be: every value is a compile-time constant of
    this deployment — scope URLs, their Google classification, and the contents
    of ``GOOGLE_OAUTH_SCOPES``. No tenant is read, so there is no organization to
    scope the answer to. The UI needs it *before* the user is anywhere near a
    connection, which is exactly what stops the browser from hardcoding its own
    scope list and drifting from the server's.
    """
    return GoogleRequiredScopesResponse(
        families=google_scopes.scope_catalog(),
        requested_scopes=list(google_scopes.requested_scopes()),
        deployment_sensitivity=google_scopes.deployment_sensitivity().value,
        scope_details=[
            GoogleScopeInfo.model_validate(descriptor)
            for descriptor in google_scopes.scope_descriptors()
        ],
    )


@router.get("/authorize", response_model=GoogleAuthorizeResponse)
async def authorize_google(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> GoogleAuthorizeResponse:
    """Return the Google consent URL for the caller's organization.

    The URL carries a signed state bound to ``selected_organization_id`` and to
    the user starting the flow; the callback trusts that state and nothing else.
    ``login_hint`` only pre-selects an account in Google's chooser — the user is
    free to pick another one, so it is never stored as the connected account.
    """
    if not is_google_oauth_configured():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": (
                    "Google is not configured on this deployment "
                    "(missing GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET)."
                ),
                "requires_reconsent": False,
            },
        )

    try:
        authorization_url = build_authorization_url(
            user.selected_organization_id,
            user_id=user.id,
            login_hint=user.email or None,
        )
    except GoogleOAuthError as exc:
        raise _google_http_error(exc) from exc

    return GoogleAuthorizeResponse(
        authorization_url=authorization_url,
        redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
        expires_in_seconds=GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS,
    )


# Two paths, one handler: GOOGLE_OAUTH_REDIRECT_URI defaults to the
# ``/oauth/callback`` form, and a deployment may already have the shorter one
# registered on its Google client. Whichever is registered has to answer, and
# neither is an API a client calls, so both stay out of the OpenAPI schema.
@router.get("/oauth/callback", include_in_schema=False)
@router.get("/callback", include_in_schema=False)
async def google_oauth_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
) -> RedirectResponse:
    """Finish the consent round-trip and store the organization's tokens.

    Unauthenticated by necessity — Google's redirect carries no credentials.
    The organization comes from the encrypted, signed, time-limited ``state``,
    so a forged or replayed callback cannot attach a Google account to an
    organization that did not start the flow. Failures redirect back to the UI
    with a code instead of rendering an error page: the browser is mid-flow and
    the UI owns the messaging.
    """
    if error:
        # User declined, or Google refused (e.g. access_denied).
        logger.info(f"Google consent was not granted: {error}")
        return _ui_redirect(status="error", reason=error)

    if not code:
        return _ui_redirect(status="error", reason="missing_code")

    try:
        oauth_state = parse_oauth_state(state or "")
    except GoogleOAuthError as exc:
        logger.warning(f"Rejected a Google OAuth callback: {exc.code}")
        return _ui_redirect(status="error", reason=exc.code)

    if oauth_state.user_id is None:
        # Storing a first connection needs an owner; a state without one was not
        # minted by /authorize.
        logger.warning("Rejected a Google OAuth callback: state carries no user")
        return _ui_redirect(status="error", reason="invalid_state")

    try:
        tokens = await exchange_code(code, redirect_uri=oauth_state.redirect_uri)
        await store_credentials(
            organization_id=oauth_state.organization_id,
            user_id=oauth_state.user_id,
            tokens=tokens,
        )
    except GoogleOAuthError as exc:
        logger.error(
            f"Failed to complete the Google connection for organization "
            f"{oauth_state.organization_id}: {exc.code}"
        )
        return _ui_redirect(status="error", reason=exc.code)

    logger.info(
        f"Connected a Google account for organization {oauth_state.organization_id}"
    )
    return _ui_redirect(status="connected")


@router.get("/status", response_model=GoogleConnectionStatusResponse)
async def get_google_status(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> GoogleConnectionStatusResponse:
    """Report whether the caller's organization has a usable Google connection.

    Tokens are read to build this answer but never returned: only the connection
    state, the account when known, and the access-token expiry (which the backend
    refreshes on its own — it is not an expiry of the connection).
    """
    connection = await get_connection(user.selected_organization_id)
    oauth_config = GoogleOAuthPublicConfig(**google_oauth_public_config())

    if connection is None:
        return GoogleConnectionStatusResponse(
            connected=False, oauth_config=oauth_config
        )

    return GoogleConnectionStatusResponse(
        connected=not connection.is_revoked,
        status=connection.status,
        account_email=connection.account_email,
        expires_at=connection.expires_at,
        scopes=list(connection.scopes),
        requires_reconsent=connection.is_revoked,
        revoked_reason=connection.revoked_reason,
        oauth_config=oauth_config,
    )


@router.delete("/connection", response_model=GoogleDisconnectResponse)
async def disconnect_google(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> GoogleDisconnectResponse:
    """Revoke the grant at Google and delete the stored tokens.

    Idempotent: an organization with nothing connected gets
    ``disconnected: false`` rather than a 404, so the UI can offer the action
    without first checking. Tools bound to sheets are left in place — they simply
    stop working until the organization reconnects.
    """
    try:
        disconnected = await disconnect(user.selected_organization_id)
    except GoogleOAuthError as exc:
        raise _google_http_error(exc) from exc

    return GoogleDisconnectResponse(disconnected=disconnected)


# ---------------------------------------------------------------------------
# Sheet binding
# ---------------------------------------------------------------------------


@router.post("/sheets/bind", response_model=SheetBindResponse)
async def bind_google_sheet(
    request: SheetBindRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> SheetBindResponse:
    """Point a tool at one tab of one spreadsheet and infer what its columns mean.

    Reads the header row, asks the LLM what each column is for, caches the result
    against a fingerprint of that header row, and creates or updates a
    ``google_sheets`` tool for the caller's organization. No column is configured
    by hand.

    The response lists every understood column with its inferred description and
    the column letter the server will write to — this is what the UI shows the
    customer for confirmation.
    """
    if request.tool_uuid:
        # Re-pointing an existing binding: prove the caller owns it before the
        # service touches it.
        await _resolve_sheet_tool(request.tool_uuid, user.selected_organization_id)

    try:
        binding = await bind_sheet(
            organization_id=user.selected_organization_id,
            user_id=user.id,
            spreadsheet_id=request.spreadsheet_id,
            sheet_name=request.sheet_name,
            tool_name=request.tool_name,
            header_row=request.header_row,
            tool_uuid=request.tool_uuid,
            description=request.description,
        )
    except (GoogleOAuthError, GoogleSheetsError) as exc:
        raise _google_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        f"Bound spreadsheet {request.spreadsheet_id} tab '{request.sheet_name}' to "
        f"tool {binding.tool_uuid} for organization {user.selected_organization_id}"
    )
    return SheetBindResponse.model_validate(binding)


@router.post(
    "/sheets/{tool_uuid}/refresh-schema", response_model=SheetSchemaRefreshResponse
)
async def refresh_google_sheet_schema(
    tool_uuid: str,
    request: Optional[SheetSchemaRefreshRequest] = None,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> SheetSchemaRefreshResponse:
    """Re-infer a bound sheet's schema, but only if its header row changed.

    The fingerprint check is the point: an unchanged header row returns the
    cached schema with ``refreshed: false`` and costs no LLM call. ``force``
    re-runs the inference anyway, for a customer who edited a header's wording
    without changing the row, or who wants a second opinion on a description.
    """
    await _resolve_sheet_tool(tool_uuid, user.selected_organization_id)

    try:
        result = await refresh_sheet_schema(
            organization_id=user.selected_organization_id,
            tool_uuid=tool_uuid,
            force=bool(request.force) if request else False,
        )
    except (GoogleOAuthError, GoogleSheetsError) as exc:
        raise _google_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SheetSchemaRefreshResponse.model_validate(result)


@router.get("/sheets/{tool_uuid}/preview", response_model=SheetPreviewResponse)
async def preview_google_sheet(
    tool_uuid: str,
    rows: int = Query(default=5, ge=1, le=20, description="Data rows to return."),
    user: UserModel = Depends(get_user_with_selected_organization),
) -> SheetPreviewResponse:
    """Show the sheet as it reads right now, next to the cached schema.

    Live headers and the first rows let the customer check the inference against
    real data. ``schema_stale`` says whether the header row has drifted from the
    fingerprint the cached schema was built on — that is when the UI should offer
    to refresh.
    """
    await _resolve_sheet_tool(tool_uuid, user.selected_organization_id)

    try:
        preview = await preview_sheet(
            organization_id=user.selected_organization_id,
            tool_uuid=tool_uuid,
            row_limit=rows,
        )
    except (GoogleOAuthError, GoogleSheetsError) as exc:
        raise _google_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SheetPreviewResponse.model_validate(preview)


# ---------------------------------------------------------------------------
# Workspace overview
# ---------------------------------------------------------------------------


@router.get("/workspace/overview", response_model=WorkspaceOverviewResponse)
async def get_google_workspace_overview(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WorkspaceOverviewResponse:
    """Everything the organization's connected Google account exposes, in one call.

    Replaces the five parallel calls the UI would otherwise make after the
    consent redirect — and, unlike them, says *why* a section is empty: a scope
    that was never granted, a call that failed, or an account that genuinely has
    nothing there. A disconnected organization is not an error here: it comes
    back ``connected: false`` with empty sections.
    """
    try:
        overview = await google_workspace.build_overview(user.selected_organization_id)
    except (GoogleOAuthError, GoogleApiError) as exc:
        raise _discovery_http_error(exc) from exc

    return WorkspaceOverviewResponse.model_validate(overview)


# ---------------------------------------------------------------------------
# Resource discovery (pickers)
# ---------------------------------------------------------------------------


@router.get("/tools/sheets", response_model=list[SpreadsheetSummary])
async def list_google_spreadsheets(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[SpreadsheetSummary]:
    """Spreadsheets the connected account can reach, most recently modified first."""
    items = await _discover(
        google_drive.list_spreadsheets(user.selected_organization_id),
        what="spreadsheets",
        organization_id=user.selected_organization_id,
    )
    return [SpreadsheetSummary.model_validate(item) for item in items]


@router.get(
    "/tools/sheets/{spreadsheet_id}/tabs", response_model=list[SheetTabSummary]
)
async def list_google_sheet_tabs(
    spreadsheet_id: str,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[SheetTabSummary]:
    """Tabs of one spreadsheet, so the operator can bind the right one.

    The id comes from the URL and is passed to Google as-is: it is not a Volira
    resource, so there is nothing to own. What bounds it is the access token —
    the organization's own — and Google answers 404 for a file that account
    cannot open, which surfaces here as an empty list.
    """
    tabs = await _discover(
        google_workspace.list_sheet_tabs(
            user.selected_organization_id, spreadsheet_id
        ),
        what="sheet tabs",
        organization_id=user.selected_organization_id,
    )
    return [SheetTabSummary.model_validate(tab) for tab in tabs]


@router.get("/tools/sheets/{spreadsheet_id}/preview")
async def preview_google_spreadsheet(
    spreadsheet_id: str,
    a1_range: Optional[str] = Query(
        default=None,
        alias="range",
        description="A1 range to read, e.g. 'Leads!A1:E5'. Defaults to the first tab.",
    ),
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[list[Any]]:
    """First cells of a spreadsheet, as a check before binding it.

    Rows are ragged — Google trims trailing empty cells — so the UI must pad
    against the longest row rather than assume a rectangle.
    """
    return await _discover(
        google_workspace.preview_sheet_range(
            user.selected_organization_id, spreadsheet_id, a1_range
        ),
        what="sheet preview",
        organization_id=user.selected_organization_id,
    )


@router.get("/tools/calendar/calendars", response_model=list[CalendarSummary])
async def list_google_calendars(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[CalendarSummary]:
    """Calendars in the account's calendar list, primary one included."""
    items = await _discover(
        google_calendar.list_calendars(user.selected_organization_id),
        what="calendars",
        organization_id=user.selected_organization_id,
    )
    return [CalendarSummary.model_validate(item) for item in items]


@router.get("/tools/drive/folders", response_model=list[DriveItemSummary])
async def list_google_drive_folders(
    parent_id: Optional[str] = Query(
        default=None,
        description="Folder to list children of. Defaults to the Drive root.",
    ),
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[DriveItemSummary]:
    """Folders under one parent, for browsing Drive one level at a time."""
    items = await _discover(
        google_drive.list_folders(
            user.selected_organization_id, parent_id=parent_id or "root"
        ),
        what="Drive folders",
        organization_id=user.selected_organization_id,
    )
    return [DriveItemSummary.model_validate(item) for item in items]


@router.get("/tools/drive/files", response_model=list[DriveItemSummary])
async def list_google_drive_files(
    folder_id: Optional[str] = Query(
        default=None, description="Only files in this folder."
    ),
    mime_type: Optional[str] = Query(
        default=None,
        description="Only files of this MIME type, e.g. 'application/pdf'.",
    ),
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[DriveItemSummary]:
    """Files in a folder, optionally narrowed to one MIME type."""
    items = await _discover(
        google_drive.list_files(
            user.selected_organization_id,
            folder_id=folder_id,
            mime_type=mime_type,
        ),
        what="Drive files",
        organization_id=user.selected_organization_id,
    )
    return [DriveItemSummary.model_validate(item) for item in items]


@router.get("/tools/drive/search", response_model=list[DriveItemSummary])
async def search_google_drive(
    query: str = Query(
        min_length=1,
        description="Text to match against file names, as typed by the operator.",
    ),
    mime_type: Optional[str] = Query(
        default=None, description="Restrict the search to one MIME type."
    ),
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[DriveItemSummary]:
    """Search Drive by name — the way to find a file a folder walk would bury."""
    items = await _discover(
        google_drive.search_files(
            user.selected_organization_id, query, mime_type=mime_type
        ),
        what="Drive search",
        organization_id=user.selected_organization_id,
    )
    return [DriveItemSummary.model_validate(item) for item in items]


@router.get("/tools/gmail/labels", response_model=list[GmailLabelSummary])
async def list_google_gmail_labels(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[GmailLabelSummary]:
    """Gmail labels, system and custom, for inbox-driven automations."""
    items = await _discover(
        google_gmail.list_labels(user.selected_organization_id),
        what="Gmail labels",
        organization_id=user.selected_organization_id,
    )
    return [GmailLabelSummary.model_validate(item) for item in items]


@router.get("/tools/gmail/messages", response_model=list[GmailMessageSummary])
async def list_google_gmail_messages(
    label_id: Optional[str] = Query(
        default="INBOX", description="Label to read from. Defaults to the inbox."
    ),
    max_results: int = Query(
        default=GOOGLE_DISCOVERY_RECENT_MESSAGES,
        ge=1,
        le=50,
        description="How many messages to return.",
    ),
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[GmailMessageSummary]:
    """Headers of the most recent messages under a label — never message bodies.

    Capped low on purpose: this is a preview that tells the operator the Gmail
    connection works, not a mail client.
    """
    items = await _discover(
        google_gmail.list_messages(
            user.selected_organization_id,
            label_id=label_id or "INBOX",
            max_results=max_results,
        ),
        what="Gmail messages",
        organization_id=user.selected_organization_id,
    )
    return [GmailMessageSummary.model_validate(item) for item in items]


@router.get("/tools/docs", response_model=list[DocsSummary])
async def list_google_docs(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[DocsSummary]:
    """Google Docs documents the connected account can open."""
    items = await _discover(
        google_docs.list_documents(user.selected_organization_id),
        what="documents",
        organization_id=user.selected_organization_id,
    )
    return [DocsSummary.model_validate(item) for item in items]
