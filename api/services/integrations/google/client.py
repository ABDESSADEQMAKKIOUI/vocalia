"""Shared HTTP transport for Google's REST APIs (httpx, no SDK).

Every Google API this integration talks to — Sheets, Drive, Calendar, Gmail,
Docs — needs the same three behaviours, and all three are easy to get subtly
wrong five times over:

- **Retries.** Google's quotas are per user, per project, per minute, so a burst
  answers 429. Retryable statuses and network errors back off exponentially with
  jitter (concurrent callers hitting the same quota must not wake up together)
  and honour ``Retry-After`` when Google sends it.
- **One re-auth, not a loop.** A 401 means the access token expired between
  building the client and using it. Exactly one forced refresh + replay is
  allowed; a second 401 is an authorization failure, because retrying a token
  Google keeps rejecting is how a live call turns into a minute of silence.
- **Typed errors.** No raw ``httpx`` exception ever escapes. Callers branch on
  ``code`` / ``requires_reconsent`` / ``missing_scope``, never on message text.

Subclasses supply a base URL, a label for messages, the scopes their API needs,
and — when the API has better wording for a failure than the generic mapping —
an override of :meth:`GoogleApiClient._translate_error`.

Insufficient scope is its own outcome
-------------------------------------
A 403 that says "insufficient authentication scopes" is not a permission problem
on the resource: the grant is valid, it is just too narrow. It is translated into
:class:`GoogleScopeError` carrying ``missing_scope``, so the UI can say *which*
permission to grant instead of "Google denied access". It is deliberately **not**
flagged ``requires_reconsent``: that flag means "the grant is gone, reconnect",
which routes answer with 409, while a too-narrow grant is an HTTP 403 the caller
fixes by consenting to more (see the discovery plan §11).
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, ClassVar, Self

import httpx
from loguru import logger

from api.constants import (
    GOOGLE_API_MAX_ATTEMPTS,
    GOOGLE_API_RETRY_BASE_DELAY_SECONDS,
    GOOGLE_API_RETRY_MAX_DELAY_SECONDS,
    GOOGLE_API_TIMEOUT_SECONDS,
)

from .oauth import get_valid_access_token

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

# Google reports "your token does not carry the scope this call needs" in two
# shapes depending on the API generation: the newer google.rpc.ErrorInfo
# (``details[].reason``) and the classic ``errors[].reason``. Both are matched by
# reason rather than by message, so the Drive/Sheets 403 that really means "this
# file was never shared with the app" is not mistaken for a scope problem.
_INSUFFICIENT_SCOPE_REASONS = frozenset(
    {
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        "insufficientPermissions",
        "insufficientScopes",
    }
)
_INSUFFICIENT_SCOPE_MESSAGE = "insufficient authentication scopes"


class GoogleApiError(Exception):
    """A Google REST API request failed.

    - ``code``: machine-readable outcome. Google's own ``error.status`` when it
      sent one, otherwise the subclass default.
    - ``retryable``: the same call might succeed later; the transport has already
      exhausted its own attempts by the time this is raised.
    - ``requires_reconsent``: the grant itself is gone (revoked, expired) and no
      retry can recover it — the user must go through the consent screen again.
    - ``missing_scope``: set only for an insufficient-scope 403, naming the scope
      the call needed.

    Subclasses exist per API so a caller that only handles Sheets keeps handling
    exactly Sheets; they override ``default_code`` and nothing else, which is
    what lets an API-specific scope error inherit from both this hierarchy and
    its API's error class without an ``__init__`` conflict.
    """

    default_code: ClassVar[str] = "google_api_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool = False,
        requires_reconsent: bool = False,
        missing_scope: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.http_status = http_status
        self.retryable = retryable
        self.requires_reconsent = requires_reconsent
        self.missing_scope = missing_scope


class GoogleScopeError(GoogleApiError):
    """The organization's grant does not cover the scope this call needed."""

    default_code = "insufficient_scope"


def _retry_delay(failures: int, retry_after: str | None = None) -> float:
    """Exponential backoff with jitter, capped; honours ``Retry-After``."""
    if retry_after:
        try:
            return min(float(retry_after), GOOGLE_API_RETRY_MAX_DELAY_SECONDS)
        except ValueError:
            pass  # Retry-After can also be an HTTP date; fall back to backoff.
    delay = GOOGLE_API_RETRY_BASE_DELAY_SECONDS * (2 ** max(0, failures - 1))
    delay = min(delay, GOOGLE_API_RETRY_MAX_DELAY_SECONDS)
    # Jitter so several concurrent calls hitting the same per-minute quota do
    # not all wake up together and 429 again.
    return delay * (0.75 + random.random() * 0.5)


# How much of a non-JSON upstream body is worth keeping for an operator reading
# logs. Whole HTML error pages are noise, and truncation is what stops one
# proxy's error page from filling a log line.
_RAW_BODY_LOG_LIMIT = 500


def _parse_error_body(response: httpx.Response) -> tuple[str, str, tuple[str, ...]]:
    """Pull ``(message, status, reasons)`` out of a Google error response.

    Only Google's own structured ``error.message`` becomes the message a caller
    sees. When the body is not Google's JSON — an intercepting proxy's HTML
    error page, a captive portal, a load balancer — the raw body is logged
    (truncated) and the caller gets a generic sentence instead. Promoting that
    body to the message would forward whatever an intermediary chose to say
    straight into an API response and into the browser.
    """
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {}

    message = str(error.get("message") or "").strip()
    if not message:
        raw = (response.text or "").strip()
        if raw:
            logger.warning(
                f"Non-Google error body for {response.request.method if response.request else '?'} "
                f"{response.url} (HTTP {response.status_code}): "
                f"{raw[:_RAW_BODY_LOG_LIMIT]}"
            )
        message = f"Google returned HTTP {response.status_code}."
    status = str(error.get("status") or "").strip()

    reasons: list[str] = []
    for entry in list(error.get("details") or []) + list(error.get("errors") or []):
        if isinstance(entry, dict) and entry.get("reason"):
            reasons.append(str(entry["reason"]))
    return message, status, tuple(reasons)


def _is_insufficient_scope(message: str, reasons: tuple[str, ...]) -> bool:
    if any(reason in _INSUFFICIENT_SCOPE_REASONS for reason in reasons):
        return True
    return _INSUFFICIENT_SCOPE_MESSAGE in message.lower()


class GoogleApiClient:
    """Base class for one Google account's access to one API, for one org.

    Ownership of the resource ids passed to a subclass's methods is *not*
    checked here — the caller must have resolved them from an
    organization-scoped record first.
    """

    # Prepended to any path that is not already absolute.
    base_url: ClassVar[str] = ""
    # Used verbatim in log lines and error messages, e.g. "Google Sheets API".
    api_label: ClassVar[str] = "Google API"
    # Scopes this API needs, most representative first: used to name the scope
    # in an insufficient-scope error when the call site did not say which one.
    api_scopes: ClassVar[tuple[str, ...]] = ()
    error_class: ClassVar[type[GoogleApiError]] = GoogleApiError
    scope_error_class: ClassVar[type[GoogleApiError]] = GoogleScopeError

    def __init__(
        self,
        access_token: str,
        *,
        organization_id: int | None = None,
        timeout_seconds: float = GOOGLE_API_TIMEOUT_SECONDS,
        max_attempts: int = GOOGLE_API_MAX_ATTEMPTS,
    ):
        self._access_token = access_token
        self._organization_id = organization_id
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max(1, max_attempts)

    @property
    def organization_id(self) -> int | None:
        return self._organization_id

    @classmethod
    async def for_organization(cls, organization_id: int) -> Self:
        """Build a client from the organization's stored Google connection.

        Raises ``GoogleOAuthError`` (with ``requires_reconsent``) when the
        organization has no usable connection.
        """
        access_token = await get_valid_access_token(organization_id)
        return cls(access_token, organization_id=organization_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _resolve_url(self, url_or_path: str) -> str:
        """Absolute URLs pass through; anything else hangs off ``base_url``."""
        if url_or_path.startswith(("http://", "https://")):
            return url_or_path
        return f"{self.base_url}{url_or_path}"

    async def _request(
        self,
        method: str,
        url_or_path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        required_scope: str | None = None,
    ) -> dict[str, Any]:
        """Issue one request, retrying transient failures.

        Retries 429/5xx and network errors with backed-off, jittered delays. A
        401 triggers exactly one forced token refresh + replay (the access token
        may have expired between building the client and using it); a second 401
        is reported as an authorization failure. ``required_scope`` names the
        scope to blame if Google answers 403 insufficient scope.
        """
        url = self._resolve_url(url_or_path)
        failures = 0
        reauthenticated = False

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            while True:
                try:
                    response = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                    )
                except httpx.HTTPError as exc:
                    failures += 1
                    if failures >= self._max_attempts:
                        raise self.error_class(
                            f"Network error calling the {self.api_label}: {exc}",
                            code="network_error",
                            retryable=True,
                        ) from exc
                    await asyncio.sleep(_retry_delay(failures))
                    continue

                if (
                    response.status_code == 401
                    and not reauthenticated
                    and self._organization_id is not None
                ):
                    reauthenticated = True
                    self._access_token = await get_valid_access_token(
                        self._organization_id, force_refresh=True
                    )
                    logger.debug(
                        f"Refreshed the Google access token for organization "
                        f"{self._organization_id} after a 401 and retried {method} "
                        f"{url_or_path}"
                    )
                    continue

                if response.status_code in RETRYABLE_STATUSES:
                    failures += 1
                    if failures < self._max_attempts:
                        delay = _retry_delay(
                            failures, response.headers.get("Retry-After")
                        )
                        logger.warning(
                            f"{self.api_label} returned {response.status_code} for "
                            f"{method} {url_or_path}; retrying in {delay:.1f}s "
                            f"({failures}/{self._max_attempts})"
                        )
                        await asyncio.sleep(delay)
                        continue

                if response.status_code >= 400:
                    raise self._error_from_response(
                        response,
                        method=method,
                        path=url_or_path,
                        required_scope=required_scope,
                    )

                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                return payload if isinstance(payload, dict) else {}

    def _error_from_response(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
        required_scope: str | None = None,
    ) -> GoogleApiError:
        """Turn a Google error body into a typed, actionable exception."""
        status_code = response.status_code
        message, status, reasons = _parse_error_body(response)

        if status_code == 403 and _is_insufficient_scope(message, reasons):
            return self._scope_error(required_scope)

        return self._translate_error(
            status_code=status_code,
            message=message,
            reason=status,
            method=method,
            path=path,
        )

    def _scope_error(self, required_scope: str | None) -> GoogleApiError:
        """Name the scope the call needed, so the UI can ask for that one."""
        scope = required_scope or (self.api_scopes[0] if self.api_scopes else None)
        message = (
            f"Missing required Google scope: {scope}"
            if scope
            else (
                f"The connected Google account has not granted the permissions "
                f"the {self.api_label} needs."
            )
        )
        return self.scope_error_class(
            message,
            http_status=403,
            missing_scope=scope,
        )

    def _translate_error(
        self,
        *,
        status_code: int,
        message: str,
        reason: str,
        method: str,
        path: str,
    ) -> GoogleApiError:
        """Map a non-scope HTTP failure onto this API's error class.

        Subclasses override this to say something the caller can act on — the
        generic wording knows nothing about what the resource was.
        """
        if status_code in (401, 403):
            return self.error_class(
                message or "Google denied access to this resource.",
                code=reason or "permission_denied",
                http_status=status_code,
                requires_reconsent=status_code == 401,
            )
        if status_code == 404:
            return self.error_class(
                message or "The Google resource was not found.",
                code=reason or "not_found",
                http_status=status_code,
            )

        logger.error(
            f"{self.api_label} {method} {path} failed with HTTP {status_code}: "
            f"{message}"
        )
        return self.error_class(
            message or f"{self.api_label} request failed with HTTP {status_code}.",
            code=reason or self.error_class.default_code,
            http_status=status_code,
            retryable=status_code in RETRYABLE_STATUSES,
        )
