"""Google OAuth2 for the multi-tenant Google Sheets integration.

One Google Cloud OAuth client backs the whole deployment; each organization
consents on its own and gets its own token pair, stored in one
``ExternalCredentialModel`` row scoped by ``organization_id``. Every function
here takes an ``organization_id`` and every DB call is org-scoped — a Google
connection belongs to an organization, never to the deployment.

Why ``drive.file`` and not ``drive.readonly``
---------------------------------------------
The scopes requested here are:

- ``https://www.googleapis.com/auth/drive.file``
- ``https://www.googleapis.com/auth/spreadsheets``

Google classifies ``drive`` and ``drive.readonly`` as RESTRICTED scopes. An app
that requests them must pass Google's app verification *and* a yearly
third-party security assessment (CASA), which is paid, recurring, and gates the
whole OAuth client — including every other org already connected. ``drive.file``
is a NON-SENSITIVE scope: no verification, no assessment, no annual audit.

The trade-off is deliberate: ``drive.file`` only grants access to files the user
explicitly hands to the app (files created by the app, or files chosen through
the Google Picker). We cannot list a user's Drive, and we do not want to — the
owner points at one spreadsheet and the agent works with that spreadsheet. So
the browser side must open the Google Picker (see ``GOOGLE_PICKER_API_KEY`` /
``GOOGLE_PICKER_APP_ID`` in ``api/constants.py``); a spreadsheet id typed by
hand will come back 404 from the Sheets API because it was never granted.

``spreadsheets`` (read/write on Sheets) is also non-sensitive and is what lets
us read the header row and append/update rows on the picked file.

Token storage
-------------
Tokens live in ``external_credentials.credential_data``. The two secrets
(``access_token``, ``refresh_token``) are encrypted with ``api/utils/crypto.py``
(Fernet, ``enc:v1:`` prefix). The rest of the payload — status, expiry, granted
scopes — stays readable so an operator can tell whether an org's connection is
healthy without decrypting anything. Tokens are never logged, not even
truncated.

Refresh policy
--------------
``get_valid_access_token`` refreshes when the access token expires within
``GOOGLE_TOKEN_REFRESH_LEEWAY_SECONDS``. If Google answers ``invalid_grant``
(the user removed the app, the refresh token was revoked or expired), the
connection is marked ``revoked`` and the caller is told to re-consent instead of
retrying forever — a revoked grant never recovers on its own.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from loguru import logger
from sqlalchemy.exc import IntegrityError

from api.constants import (
    GOOGLE_API_TIMEOUT_SECONDS,
    GOOGLE_CREDENTIAL_NAME,
    GOOGLE_OAUTH_AUTH_URL,
    GOOGLE_OAUTH_REDIRECT_URI,
    GOOGLE_OAUTH_REVOKE_URL,
    GOOGLE_OAUTH_SCOPES,
    GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS,
    GOOGLE_OAUTH_TOKEN_URL,
    GOOGLE_TOKEN_REFRESH_LEEWAY_SECONDS,
)
from api.enums import WebhookCredentialType
from api.utils.crypto import _ENCRYPTED_PREFIX, decrypt_value, encrypt_value

# Marks the credential rows this module owns, so a Google connection is never
# confused with a hand-created webhook credential that happens to share a name.
GOOGLE_PROVIDER = "google_oauth"

# ``external_credentials.credential_type`` is a Postgres enum
# (``webhook_credential_type``); adding a ``google_oauth`` value would need a
# migration. Bearer token is the closest existing shape — but note the access
# token is NOT stored under the generic ``token`` key, so the shared
# ``build_auth_header`` helper cannot accidentally hand an OAuth token to an
# unrelated webhook.
_CREDENTIAL_TYPE = WebhookCredentialType.BEARER_TOKEN.value

STATUS_CONNECTED = "connected"
STATUS_REVOKED = "revoked"

# One refresh in flight per organization inside this worker. Google does not
# rotate refresh tokens by default, so a cross-worker race is harmless (both
# workers get a valid access token); this lock just avoids a stampede of
# identical refreshes when several concurrent calls hit an expiring token.
_refresh_locks: dict[int, asyncio.Lock] = {}


class GoogleOAuthError(Exception):
    """A Google OAuth operation failed.

    ``requires_reconsent`` is the actionable bit: when True the organization's
    grant is gone (revoked, expired, never established) and no amount of
    retrying will fix it — the user must go through the consent screen again.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "google_oauth_error",
        requires_reconsent: bool = False,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.requires_reconsent = requires_reconsent
        self.http_status = http_status


@dataclass(frozen=True)
class GoogleOAuthState:
    """Payload carried through the consent round-trip in the ``state`` param."""

    organization_id: int
    user_id: int | None
    redirect_uri: str | None
    nonce: str
    issued_at: int


@dataclass(frozen=True)
class GoogleTokens:
    """A token response from Google. ``refresh_token`` is only present on the
    initial code exchange — refreshes reuse the stored one."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class GoogleConnection:
    """An organization's stored Google grant, with secrets already decrypted."""

    credential_uuid: str
    organization_id: int
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: tuple[str, ...]
    status: str
    account_email: str | None
    revoked_reason: str | None

    @property
    def is_revoked(self) -> bool:
        return self.status == STATUS_REVOKED

    def needs_refresh(
        self, leeway_seconds: int = GOOGLE_TOKEN_REFRESH_LEEWAY_SECONDS
    ) -> bool:
        """True when the access token is missing, or expires within the leeway."""
        if not self.access_token:
            return True
        if self.expires_at is None:
            # Unknown expiry — treat as stale rather than gambling on a 401
            # in the middle of a live call.
            return True
        return datetime.now(UTC) + timedelta(seconds=leeway_seconds) >= self.expires_at


# ---------------------------------------------------------------------------
# Deployment configuration
# ---------------------------------------------------------------------------


def _client_credentials() -> tuple[str, str]:
    """Read the OAuth client credentials live from the environment."""
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise GoogleOAuthError(
            "Google integration is not configured on this deployment "
            "(missing GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET).",
            code="not_configured",
        )
    return client_id, client_secret


def is_google_oauth_configured() -> bool:
    """True when this deployment can run the Google consent flow at all."""
    return bool(
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
        and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    )


def google_oauth_public_config() -> dict[str, Any]:
    """Browser-safe config the UI needs to render the connect button and open
    the Google Picker. The client id and Picker API key are public by design;
    the client secret is never exposed here."""
    return {
        "enabled": is_google_oauth_configured(),
        "client_id": os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or None,
        "picker_api_key": os.environ.get("GOOGLE_PICKER_API_KEY") or None,
        "picker_app_id": os.environ.get("GOOGLE_PICKER_APP_ID") or None,
        "scopes": list(GOOGLE_OAUTH_SCOPES),
    }


# ---------------------------------------------------------------------------
# Authorization URL + signed state
# ---------------------------------------------------------------------------


def build_oauth_state(
    organization_id: int,
    *,
    user_id: int | None = None,
    redirect_uri: str | None = None,
) -> str:
    """Mint an opaque, tamper-proof ``state`` bound to one organization.

    The state is encrypted with the deployment's Fernet key, so the callback
    recovers the organization from the state itself instead of trusting a query
    parameter — a user cannot swap in another org's id, and a replayed state
    from another deployment will not decrypt. It doubles as the CSRF token
    (random ``nonce``) and carries the ``redirect_uri`` so the token exchange
    can replay exactly the value Google saw.
    """
    payload = {
        "organization_id": int(organization_id),
        "user_id": int(user_id) if user_id is not None else None,
        "redirect_uri": redirect_uri,
        "nonce": secrets.token_urlsafe(16),
        "issued_at": int(time.time()),
    }
    return encrypt_value(json.dumps(payload, separators=(",", ":")))


def parse_oauth_state(state: str) -> GoogleOAuthState:
    """Verify and decode a ``state`` produced by :func:`build_oauth_state`.

    Raises ``GoogleOAuthError`` when the state is missing, tampered with,
    unreadable, or older than ``GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS``.
    """
    if not state:
        raise GoogleOAuthError(
            "Missing OAuth state.", code="invalid_state", http_status=400
        )
    # decrypt_value passes non-prefixed input straight through, for legacy
    # plaintext credentials. That is harmless for a stored secret and fatal
    # here: without this check a hand-written state such as
    # {"organization_id": 42, ...} would be accepted verbatim, letting anyone
    # attach THEIR Google account to SOMEONE ELSE'S organization. The state is
    # the only thing carrying the organization through the consent round trip,
    # so it must be proven to be ours before a single field is read.
    if not state.startswith(_ENCRYPTED_PREFIX):
        raise GoogleOAuthError(
            "Invalid OAuth state.", code="invalid_state", http_status=400
        )
    try:
        payload = json.loads(decrypt_value(state))
        organization_id = int(payload["organization_id"])
        issued_at = int(payload["issued_at"])
        nonce = str(payload["nonce"])
    except Exception as exc:
        raise GoogleOAuthError(
            "Invalid OAuth state.", code="invalid_state", http_status=400
        ) from exc

    if int(time.time()) - issued_at > GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS:
        raise GoogleOAuthError(
            "The Google authorization link expired. Start the connection again.",
            code="expired_state",
            http_status=400,
        )

    user_id = payload.get("user_id")
    return GoogleOAuthState(
        organization_id=organization_id,
        user_id=int(user_id) if user_id is not None else None,
        redirect_uri=payload.get("redirect_uri") or None,
        nonce=nonce,
        issued_at=issued_at,
    )


def build_authorization_url(
    organization_id: int,
    redirect_uri: str | None = None,
    state: str | None = None,
    *,
    user_id: int | None = None,
    login_hint: str | None = None,
) -> str:
    """Build the Google consent URL for one organization.

    ``access_type=offline`` is what makes Google issue a refresh token, and
    ``prompt=consent`` forces the consent screen every time: on a second
    authorization for an already-granted account Google silently omits the
    refresh token, which would leave the connection unable to survive the first
    hour. Paying for one extra consent click is worth a connection that keeps
    working.

    When ``state`` is omitted a signed state is minted from ``organization_id``
    (see :func:`build_oauth_state`), so the callback needs no server-side
    session to know which organization consented.
    """
    client_id, _ = _client_credentials()
    resolved_redirect = redirect_uri or GOOGLE_OAUTH_REDIRECT_URI
    resolved_state = state or build_oauth_state(
        organization_id, user_id=user_id, redirect_uri=resolved_redirect
    )

    params = {
        "client_id": client_id,
        "redirect_uri": resolved_redirect,
        "response_type": "code",
        "scope": " ".join(GOOGLE_OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": resolved_state,
    }
    if login_hint:
        params["login_hint"] = login_hint

    logger.info(
        f"Built Google authorization URL for organization {organization_id} "
        f"(redirect_uri={resolved_redirect})"
    )
    return f"{GOOGLE_OAUTH_AUTH_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


async def _token_request(form: dict[str, str]) -> GoogleTokens:
    """POST to Google's token endpoint and parse the response.

    Never logs the form or the response body: both carry secrets.
    """
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_API_TIMEOUT_SECONDS) as client:
            response = await client.post(GOOGLE_OAUTH_TOKEN_URL, data=form)
    except httpx.HTTPError as exc:
        raise GoogleOAuthError(
            f"Network error calling the Google token endpoint: {exc}",
            code="network_error",
        ) from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    if response.status_code >= 400:
        error = str(payload.get("error") or "token_request_failed")
        description = str(payload.get("error_description") or "").strip()
        # invalid_grant is terminal: the refresh token was revoked, expired, or
        # the authorization code was already used. Retrying cannot help.
        requires_reconsent = error == "invalid_grant"
        raise GoogleOAuthError(
            f"Google rejected the token request ({error})"
            + (f": {description}" if description else ""),
            code=error,
            requires_reconsent=requires_reconsent,
            http_status=response.status_code,
        )

    access_token = payload.get("access_token")
    if not access_token:
        raise GoogleOAuthError(
            "Google token response contained no access_token.",
            code="malformed_token_response",
        )

    try:
        expires_in = int(payload.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    # Google's access tokens last an hour; fall back to that when the field is
    # missing rather than treating the token as immediately stale.
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in or 3600)

    scope_value = payload.get("scope") or ""
    scopes = tuple(s for s in str(scope_value).split(" ") if s)

    return GoogleTokens(
        access_token=str(access_token),
        refresh_token=payload.get("refresh_token") or None,
        expires_at=expires_at,
        scopes=scopes or GOOGLE_OAUTH_SCOPES,
    )


async def exchange_code(code: str, redirect_uri: str | None = None) -> GoogleTokens:
    """Exchange an authorization code for tokens.

    ``redirect_uri`` must be byte-identical to the one used in the
    authorization URL — Google validates it again here. It defaults to
    ``GOOGLE_OAUTH_REDIRECT_URI``; pass the value recovered from the signed
    state when the callback route uses a per-request redirect.
    """
    client_id, client_secret = _client_credentials()
    tokens = await _token_request(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri or GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    )
    if not tokens.refresh_token:
        # With access_type=offline + prompt=consent this should not happen; if
        # it does, the connection would die in an hour with no way back.
        raise GoogleOAuthError(
            "Google returned no refresh token. Remove Volira from the Google "
            "account's third-party access and connect again.",
            code="missing_refresh_token",
            requires_reconsent=True,
        )
    return tokens


async def refresh_access_token(refresh_token: str) -> GoogleTokens:
    """Trade a refresh token for a fresh access token.

    Google normally does not return a new refresh token here, so the caller
    keeps the stored one (:func:`_persist_tokens` handles that).
    """
    client_id, client_secret = _client_credentials()
    return await _token_request(
        {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        }
    )


# ---------------------------------------------------------------------------
# Credential storage (organization-scoped)
# ---------------------------------------------------------------------------


def _parse_expires_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _to_connection(credential: Any) -> GoogleConnection:
    """Decrypt a stored credential row into a usable connection."""
    data = credential.credential_data or {}
    scopes = data.get("scopes") or []
    stored_access_token = data.get("access_token") or ""
    stored_refresh_token = data.get("refresh_token") or ""
    return GoogleConnection(
        credential_uuid=credential.credential_uuid,
        organization_id=credential.organization_id,
        access_token=decrypt_value(stored_access_token) if stored_access_token else "",
        refresh_token=(
            decrypt_value(stored_refresh_token) if stored_refresh_token else None
        ),
        expires_at=_parse_expires_at(data.get("expires_at")),
        scopes=tuple(str(s) for s in scopes if s),
        status=str(data.get("status") or STATUS_CONNECTED),
        account_email=data.get("account_email") or None,
        revoked_reason=data.get("revoked_reason") or None,
    )


def _build_credential_data(
    *,
    tokens: GoogleTokens,
    refresh_token: str | None,
    account_email: str | None,
) -> dict[str, Any]:
    """Serialize tokens for storage. Only the two secrets are encrypted; the
    operational metadata stays readable so connection health can be inspected
    without the encryption key."""
    return {
        "provider": GOOGLE_PROVIDER,
        "status": STATUS_CONNECTED,
        "access_token": encrypt_value(tokens.access_token),
        "refresh_token": encrypt_value(refresh_token) if refresh_token else None,
        "expires_at": tokens.expires_at.isoformat(),
        "scopes": list(tokens.scopes),
        "account_email": account_email,
        "revoked_reason": None,
        "updated_at": datetime.now(UTC).isoformat(),
    }


async def get_connection(organization_id: int) -> GoogleConnection | None:
    """Return the organization's Google connection, or None if it has none.

    The listing is org-scoped at the query level; the provider marker only
    picks this module's row out of that organization's own credentials.
    """
    from api.db import db_client

    credentials = await db_client.get_credentials_for_organization(organization_id)
    matches = [
        credential
        for credential in credentials
        if (credential.credential_data or {}).get("provider") == GOOGLE_PROVIDER
    ]
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            f"Organization {organization_id} has {len(matches)} Google connections; "
            f"using {matches[0].credential_uuid}"
        )
    return _to_connection(matches[0])


async def store_credentials(
    *,
    organization_id: int,
    user_id: int,
    tokens: GoogleTokens,
    account_email: str | None = None,
) -> GoogleConnection:
    """Persist a freshly consented grant for an organization (upsert).

    Re-connecting replaces the existing row rather than creating a second one:
    ``external_credentials`` has a UNIQUE(organization_id, name), and one Google
    account per organization is the model this integration exposes.
    """
    from api.db import db_client

    credential_data = _build_credential_data(
        tokens=tokens,
        refresh_token=tokens.refresh_token,
        account_email=account_email,
    )
    description = (
        "Google Sheets connection used by agent tools. Managed automatically: "
        "re-connect from the integrations page to replace it."
    )

    existing = await get_connection(organization_id)
    if existing is not None:
        updated = await db_client.update_credential(
            credential_uuid=existing.credential_uuid,
            organization_id=organization_id,
            credential_data=credential_data,
        )
        if updated is None:
            raise GoogleOAuthError(
                "Google connection disappeared while it was being updated.",
                code="storage_failed",
            )
        logger.info(
            f"Replaced Google connection {existing.credential_uuid} for "
            f"organization {organization_id} (scopes={list(tokens.scopes)})"
        )
        return _to_connection(updated)

    try:
        created = await db_client.create_credential(
            organization_id=organization_id,
            user_id=user_id,
            name=GOOGLE_CREDENTIAL_NAME,
            credential_type=_CREDENTIAL_TYPE,
            credential_data=credential_data,
            description=description,
        )
    except IntegrityError as exc:
        # UNIQUE(organization_id, name): the organization already has an
        # unrelated credential under this name.
        raise GoogleOAuthError(
            f"This organization already has a credential named "
            f"'{GOOGLE_CREDENTIAL_NAME}'. Rename it before connecting Google.",
            code="credential_name_conflict",
        ) from exc
    logger.info(
        f"Stored Google connection {created.credential_uuid} for organization "
        f"{organization_id} (scopes={list(tokens.scopes)})"
    )
    return _to_connection(created)


async def _persist_tokens(
    *,
    organization_id: int,
    connection: GoogleConnection,
    tokens: GoogleTokens,
) -> GoogleConnection:
    """Write refreshed tokens back, keeping the stored refresh token when
    Google did not issue a new one."""
    from api.db import db_client

    credential_data = _build_credential_data(
        tokens=tokens,
        refresh_token=tokens.refresh_token or connection.refresh_token,
        account_email=connection.account_email,
    )
    updated = await db_client.update_credential(
        credential_uuid=connection.credential_uuid,
        organization_id=organization_id,
        credential_data=credential_data,
    )
    if updated is None:
        raise GoogleOAuthError(
            "Google connection disappeared while its token was being refreshed.",
            code="storage_failed",
        )
    logger.debug(
        f"Refreshed Google access token for organization {organization_id} "
        f"(expires_at={tokens.expires_at.isoformat()})"
    )
    return _to_connection(updated)


async def mark_connection_revoked(organization_id: int, *, reason: str) -> None:
    """Flag an organization's connection as revoked.

    Called when Google answers ``invalid_grant``: the grant is gone for good, so
    the row is kept (the UI needs to show "reconnect required") but marked so
    callers stop hammering the token endpoint on every turn of a conversation.
    """
    from api.db import db_client

    connection = await get_connection(organization_id)
    if connection is None:
        return

    credential = await db_client.get_credential_by_uuid(
        connection.credential_uuid, organization_id
    )
    if credential is None:
        return

    credential_data = dict(credential.credential_data or {})
    credential_data["status"] = STATUS_REVOKED
    credential_data["revoked_reason"] = reason
    credential_data["updated_at"] = datetime.now(UTC).isoformat()
    await db_client.update_credential(
        credential_uuid=connection.credential_uuid,
        organization_id=organization_id,
        credential_data=credential_data,
    )
    logger.warning(
        f"Marked Google connection {connection.credential_uuid} revoked for "
        f"organization {organization_id} (reason={reason})"
    )


def _refresh_lock(organization_id: int) -> asyncio.Lock:
    lock = _refresh_locks.get(organization_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[organization_id] = lock
    return lock


async def get_valid_access_token(
    organization_id: int, *, force_refresh: bool = False
) -> str:
    """Return an access token that is usable right now for this organization.

    Refreshes when the stored token expires within
    ``GOOGLE_TOKEN_REFRESH_LEEWAY_SECONDS``. Raises ``GoogleOAuthError`` with
    ``requires_reconsent=True`` when the organization has no connection, or when
    Google has revoked the grant — in that case the connection is marked revoked
    so the next call fails fast instead of re-attempting the refresh.
    """
    connection = await get_connection(organization_id)
    if connection is None:
        raise GoogleOAuthError(
            "No Google account is connected for this organization.",
            code="not_connected",
            requires_reconsent=True,
        )
    if connection.is_revoked:
        raise GoogleOAuthError(
            "The Google connection for this organization was revoked. "
            "Reconnect the Google account to continue.",
            code="connection_revoked",
            requires_reconsent=True,
        )
    if not force_refresh and not connection.needs_refresh():
        return connection.access_token

    async with _refresh_lock(organization_id):
        # Re-read under the lock: a concurrent caller may already have
        # refreshed while this one was waiting.
        fresh = await get_connection(organization_id)
        if fresh is None:
            raise GoogleOAuthError(
                "The Google connection was removed while refreshing its token.",
                code="not_connected",
                requires_reconsent=True,
            )
        if fresh.is_revoked:
            raise GoogleOAuthError(
                "The Google connection for this organization was revoked. "
                "Reconnect the Google account to continue.",
                code="connection_revoked",
                requires_reconsent=True,
            )
        if not fresh.needs_refresh():
            rotated_by_another_caller = fresh.access_token != connection.access_token
            if not force_refresh or rotated_by_another_caller:
                return fresh.access_token

        if not fresh.refresh_token:
            await mark_connection_revoked(
                organization_id, reason="missing_refresh_token"
            )
            raise GoogleOAuthError(
                "The stored Google connection has no refresh token. "
                "Reconnect the Google account to continue.",
                code="missing_refresh_token",
                requires_reconsent=True,
            )

        try:
            tokens = await refresh_access_token(fresh.refresh_token)
        except GoogleOAuthError as exc:
            if exc.requires_reconsent:
                await mark_connection_revoked(organization_id, reason=exc.code)
            raise

        refreshed = await _persist_tokens(
            organization_id=organization_id, connection=fresh, tokens=tokens
        )
        return refreshed.access_token


async def disconnect(organization_id: int) -> bool:
    """Revoke the grant at Google and drop the stored connection.

    The revoke call is best-effort — if Google is unreachable, or the grant is
    already gone, the local row is still removed so the organization can start
    a clean consent flow. Returns False when there was nothing to disconnect.
    """
    from api.db import db_client

    connection = await get_connection(organization_id)
    if connection is None:
        return False

    token = connection.refresh_token or connection.access_token
    if token:
        try:
            async with httpx.AsyncClient(timeout=GOOGLE_API_TIMEOUT_SECONDS) as client:
                await client.post(GOOGLE_OAUTH_REVOKE_URL, data={"token": token})
        except httpx.HTTPError as exc:
            logger.warning(
                f"Could not revoke the Google grant for organization "
                f"{organization_id} at Google: {exc}"
            )

    # delete_credential is a SOFT delete: the row survives with is_active=False,
    # still holding UNIQUE(organization_id, name). get_connection only looks at
    # active rows, so a later reconnect would take the create path and collide
    # with this very row — leaving the organization permanently unable to
    # reconnect Google. Renaming first frees the name while keeping the audit
    # trail of what was disconnected and when.
    retired_name = (
        f"{GOOGLE_CREDENTIAL_NAME} (disconnected "
        f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')})"
    )
    await db_client.update_credential(
        credential_uuid=connection.credential_uuid,
        organization_id=organization_id,
        name=retired_name,
    )

    deleted = await db_client.delete_credential(
        connection.credential_uuid, organization_id
    )
    logger.info(
        f"Disconnected Google for organization {organization_id} "
        f"(credential={connection.credential_uuid}, deleted={deleted})"
    )
    return deleted
