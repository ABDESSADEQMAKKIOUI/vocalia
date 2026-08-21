"""Meta Messenger (Facebook Page) Send API client.

One client wraps a single Page access token. Outbound replies use the
Send API (``POST /me/messages``) with ``messaging_type: RESPONSE`` (always
within the 24h standard messaging window since we only ever reply to an
inbound user message). Sender actions (mark_seen / typing) are best-effort.
"""

from __future__ import annotations

import aiohttp
from loguru import logger

from api.utils.crypto import decrypt_credentials

_GRAPH = "https://graph.facebook.com"
_DEFAULT_API_VERSION = "v23.0"

# HTTP 5xx / rate-limit codes worth a deferred retry rather than a drop.
_RETRYABLE_CODES = {613}


class MessengerApiError(Exception):
    """A Send API call failed. ``retry_kind`` is 'backoff' or 'never'."""

    def __init__(self, message: str, *, code=None, retry_kind: str = "never"):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retry_kind = retry_kind


class MessengerClient:
    def __init__(
        self,
        page_access_token: str,
        page_id: str | None = None,
        api_version: str = _DEFAULT_API_VERSION,
    ):
        self.page_access_token = page_access_token
        self.page_id = page_id
        self.api_version = api_version

    async def _post(self, path: str, payload: dict) -> dict:
        url = f"{_GRAPH}/{self.api_version}/{path}"
        params = {"access_token": self.page_access_token}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    params=params,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status >= 400:
                        err = (data or {}).get("error", {}) if isinstance(data, dict) else {}
                        code = err.get("code")
                        retry_kind = (
                            "backoff"
                            if resp.status >= 500 or code in _RETRYABLE_CODES
                            else "never"
                        )
                        raise MessengerApiError(
                            err.get("message") or f"HTTP {resp.status}",
                            code=code,
                            retry_kind=retry_kind,
                        )
                    return data or {}
        except aiohttp.ClientError as e:
            raise MessengerApiError(
                f"Network error calling Messenger Send API: {e}", retry_kind="backoff"
            ) from e

    async def send_text(self, psid: str, text: str) -> str:
        data = await self._post(
            "me/messages",
            {
                "recipient": {"id": psid},
                "messaging_type": "RESPONSE",
                "message": {"text": text},
            },
        )
        return (data or {}).get("message_id", "")

    async def send_action(self, psid: str, action: str) -> None:
        """Best-effort sender action: 'mark_seen' | 'typing_on' | 'typing_off'."""
        try:
            await self._post(
                "me/messages", {"recipient": {"id": psid}, "sender_action": action}
            )
        except Exception as e:
            logger.debug(f"Messenger sender_action {action} failed for {psid}: {e}")


def client_for_configuration(config, page_id: str | None = None) -> MessengerClient:
    """Build a MessengerClient from a MessagingConfigurationModel row."""
    credentials = decrypt_credentials(config.credentials or {})
    token = credentials.get("page_access_token") or credentials.get("access_token")
    if not token:
        raise ValueError(
            f"Messaging configuration {getattr(config, 'id', '?')} has no "
            "page_access_token credential"
        )
    return MessengerClient(page_access_token=token, page_id=page_id)
