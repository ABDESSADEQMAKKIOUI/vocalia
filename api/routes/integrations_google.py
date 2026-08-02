"""Google connection and Google Sheets binding endpoints.

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

Route handlers stay thin — they resolve the caller's ``organization_id``,
validate ownership of the referenced tool, delegate to
``services/integrations/google/``, and translate typed integration errors into
HTTP status codes.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from loguru import logger

from api.constants import (
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
from api.services.auth.depends import get_user_with_selected_organization
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


def _google_http_error(exc: GoogleOAuthError | GoogleSheetsError) -> HTTPException:
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
