"""WhatsApp Cloud API (Meta Graph API) HTTP client.

Thin async wrapper around the Graph API endpoints used by the WhatsApp
messaging channel: message sends, media upload/download, read receipts,
and WABA-scoped template management.

The client performs NO retries — callers own the retry policy and use
``WhatsAppApiError.retry_kind`` to decide what to do with a failure.

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import aiohttp
from loguru import logger

from api.utils.crypto import decrypt_credentials

GRAPH_API_BASE_URL = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v23.0"
REQUEST_TIMEOUT_SECONDS = 30

# Graph API error codes → retry semantics.
# https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
_BACKOFF_CODES = frozenset(
    {
        130429,  # Rate limit hit (throughput)
        131056,  # (Business, consumer) pair rate limit hit
        131000,  # Something went wrong (generic transient failure)
        131016,  # Service unavailable / overloaded
    }
)
_COOLDOWN_24H_CODES = frozenset(
    {
        131049,  # Per-user marketing template limit — retry after >24h
    }
)
_SUPPRESS_PERMANENT_CODES = frozenset(
    {
        131050,  # User stopped receiving marketing messages — never again
    }
)
_CAMPAIGN_HALT_CODES = frozenset(
    {
        131048,  # Spam rate limit hit — sending paused for quality reasons
        132015,  # Template is paused due to low quality
        132016,  # Template is disabled due to low quality
    }
)
_NEVER_CODES = frozenset(
    {
        100,  # Invalid parameter
        131026,  # Message undeliverable (invalid/incapable recipient)
        131047,  # Re-engagement required (24h service window closed)
        132000,  # Template parameter count mismatch
        132001,  # Template does not exist in the given language
    }
)


def _retry_kind_for(code: int | None, http_status: int | None) -> str:
    """Map a Graph error code / HTTP status to a caller-facing retry policy."""
    if code in _BACKOFF_CODES:
        return "backoff"
    if code in _COOLDOWN_24H_CODES:
        return "cooldown_24h"
    if code in _SUPPRESS_PERMANENT_CODES:
        return "suppress_permanent"
    if code in _CAMPAIGN_HALT_CODES:
        return "campaign_halt"
    if code in _NEVER_CODES:
        return "never"
    if http_status is not None and http_status >= 500:
        return "backoff"
    return "never"


class WhatsAppApiError(Exception):
    """Raised for any failed WhatsApp Cloud API request.

    ``retry_kind`` tells callers what to do with the failure:
    - "backoff": transient — retry with exponential backoff
    - "cooldown_24h": per-user marketing cap — retry after >24 hours
    - "suppress_permanent": recipient opted out — suppress forever
    - "campaign_halt": account/template quality issue — halt the campaign
    - "never": permanent request error — do not retry
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        http_status: int | None = None,
        retry_kind: str = "never",
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.http_status = http_status
        self.retry_kind = retry_kind


def verify_webhook_signature(
    app_secret: str, raw_body: bytes, signature_header: str | None
) -> bool:
    """Verify Meta's X-Hub-Signature-256 webhook signature (constant-time).

    The header carries ``sha256=<hex hmac of the raw body keyed by the app
    secret>``. Returns False when the header (or the secret) is missing.
    """
    if not signature_header or not app_secret:
        return False
    expected = (
        "sha256=" + hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature_header.strip())


class WhatsAppClient:
    """Async client for one WhatsApp Cloud API credential set.

    ``phone_number_id`` is required for message/media operations;
    ``waba_id`` is required for template management.
    """

    def __init__(
        self,
        access_token: str,
        phone_number_id: str | None = None,
        waba_id: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
    ):
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.waba_id = waba_id
        self.api_version = api_version

    # ------------------------------------------------------------------
    # Message sends
    # ------------------------------------------------------------------

    async def send_text(
        self,
        to: str,
        body: str,
        *,
        preview_url: bool = False,
        reply_to_wamid: str | None = None,
    ) -> str:
        payload = self._message_envelope(to)
        payload["type"] = "text"
        payload["text"] = {"preview_url": preview_url, "body": body}
        if reply_to_wamid:
            payload["context"] = {"message_id": reply_to_wamid}
        return await self._send_message(payload)

    async def send_template(
        self, to: str, *, name: str, language: str, components: list[dict]
    ) -> str:
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if components:
            template["components"] = components
        payload = self._message_envelope(to)
        payload["type"] = "template"
        payload["template"] = template
        return await self._send_message(payload)

    async def send_audio(
        self, to: str, *, media_id: str | None = None, link: str | None = None
    ) -> str:
        payload = self._message_envelope(to)
        payload["type"] = "audio"
        payload["audio"] = self._media_object(media_id, link)
        return await self._send_message(payload)

    async def send_image(
        self,
        to: str,
        *,
        media_id: str | None = None,
        link: str | None = None,
        caption: str | None = None,
    ) -> str:
        payload = self._message_envelope(to)
        payload["type"] = "image"
        payload["image"] = self._media_object(media_id, link, caption=caption)
        return await self._send_message(payload)

    async def send_interactive_buttons(
        self,
        to: str,
        body: str,
        buttons: list[dict],
        *,
        header: str | None = None,
        footer: str | None = None,
    ) -> str:
        """Send a reply-buttons interactive message.

        ``buttons`` is a list of {"id": ..., "title": ...} dicts (max 3).
        """
        if not buttons or len(buttons) > 3:
            raise ValueError("WhatsApp interactive messages need 1 to 3 buttons")
        try:
            action_buttons = [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                for b in buttons
            ]
        except KeyError as e:
            raise ValueError(f"Each button needs 'id' and 'title' keys: missing {e}") from e

        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": action_buttons},
        }
        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}

        payload = self._message_envelope(to)
        payload["type"] = "interactive"
        payload["interactive"] = interactive
        return await self._send_message(payload)

    async def mark_read(self, wamid: str, *, typing: bool = True) -> None:
        """Mark an inbound message as read (optionally showing a typing indicator).

        Best-effort UX call — never raises, logs a warning on failure.
        """
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": wamid,
        }
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        try:
            await self._request("POST", self._phone_path("messages"), json_body=payload)
        except Exception as e:
            logger.warning(f"Failed to mark WhatsApp message {wamid} as read: {e}")

    # ------------------------------------------------------------------
    # Embedded Signup / onboarding
    # ------------------------------------------------------------------

    @classmethod
    async def exchange_oauth_code(
        cls,
        app_id: str,
        app_secret: str,
        code: str,
        *,
        graph_version: str = DEFAULT_API_VERSION,
    ) -> str:
        """Exchange a short-lived Embedded Signup OAuth code for a long-lived
        business-integration access token.

        ``GET {base}/{graph_version}/oauth/access_token
              ?client_id=&client_secret=&code=`` — the app credentials travel
        as query params, so no bearer token is sent. Raises WhatsAppApiError
        on any non-2xx response, network failure, or a body without a token.
        """
        url = f"{GRAPH_API_BASE_URL}/{graph_version}/oauth/access_token"
        params = {
            "client_id": app_id,
            "client_secret": app_secret,
            "code": code,
        }
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request("GET", url, params=params) as response:
                    status = response.status
                    text = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise WhatsAppApiError(
                f"Network error exchanging WhatsApp OAuth code: {e}",
                retry_kind="backoff",
            ) from e

        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {}

        if not 200 <= status < 300:
            cls._raise_api_error(status, payload, fallback_text=text)

        access_token = (
            payload.get("access_token") if isinstance(payload, dict) else None
        )
        if not access_token:
            raise WhatsAppApiError(
                "WhatsApp OAuth code exchange returned no access_token",
                retry_kind="never",
            )
        return access_token

    async def get_phone_number_info(self, phone_number_id: str) -> dict:
        """Fetch a business phone number's metadata.

        Returns the Graph node with ``display_phone_number``, ``verified_name``
        and ``id`` fields (uses ``self.access_token``).
        """
        return await self._request(
            "GET",
            str(phone_number_id),
            params={"fields": "display_phone_number,verified_name,id"},
        )

    async def subscribe_app_to_waba(self, waba_id: str) -> dict:
        """Subscribe this app to the WABA's webhooks so the account delivers
        message/status callbacks. ``POST {waba_id}/subscribed_apps``."""
        return await self._request("POST", f"{waba_id}/subscribed_apps")

    async def register_phone_number(self, phone_number_id: str, pin: str) -> dict:
        """Register a phone number for Cloud API messaging, setting its
        two-step-verification PIN. ``POST {phone_number_id}/register``."""
        return await self._request(
            "POST",
            f"{phone_number_id}/register",
            json_body={"messaging_product": "whatsapp", "pin": pin},
        )

    # ------------------------------------------------------------------
    # Media
    # ------------------------------------------------------------------

    async def upload_media(self, data: bytes, mime_type: str) -> str:
        """Upload media bytes; returns the Meta media id."""
        form = aiohttp.FormData()
        form.add_field("file", data, content_type=mime_type, filename="upload")
        form.add_field("type", mime_type)
        form.add_field("messaging_product", "whatsapp")
        result = await self._request("POST", self._phone_path("media"), data=form)
        media_id = result.get("id")
        if not media_id:
            raise WhatsAppApiError(
                "WhatsApp media upload response had no media id", retry_kind="never"
            )
        return media_id

    async def get_media_info(self, media_id: str) -> dict:
        """Fetch media metadata: {url, mime_type, sha256, file_size, id}."""
        return await self._request("GET", media_id)

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Resolve a media id to its content. Returns (bytes, mime_type).

        The download URL returned by the Graph API must be fetched with the
        same Bearer token and expires after a few minutes.
        """
        info = await self.get_media_info(media_id)
        url = info.get("url")
        if not url:
            raise WhatsAppApiError(
                f"WhatsApp media {media_id} metadata had no download URL",
                retry_kind="never",
            )
        body, content_type = await self._download(url)
        mime_type = info.get("mime_type") or content_type or "application/octet-stream"
        return body, mime_type

    # ------------------------------------------------------------------
    # Template management (WABA-scoped)
    # ------------------------------------------------------------------

    async def create_template(
        self,
        *,
        name: str,
        language: str,
        category: str,
        components: list[dict],
        parameter_format: str = "positional",
    ) -> dict:
        payload: dict[str, Any] = {
            "name": name,
            "language": language,
            "category": category,
            "components": components,
            "parameter_format": parameter_format.upper(),
        }
        return await self._request(
            "POST", self._waba_path("message_templates"), json_body=payload
        )

    async def list_templates(
        self, *, status: str | None = None, after: str | None = None
    ) -> dict:
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        if after:
            params["after"] = after
        return await self._request(
            "GET", self._waba_path("message_templates"), params=params
        )

    async def update_template(
        self,
        meta_template_id: str,
        *,
        components: list[dict] | None = None,
        category: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {}
        if components is not None:
            payload["components"] = components
        if category is not None:
            payload["category"] = category
        if not payload:
            raise ValueError("update_template requires components or category")
        return await self._request("POST", str(meta_template_id), json_body=payload)

    async def delete_template(self, *, name: str, hsm_id: str | None = None) -> dict:
        params: dict[str, str] = {"name": name}
        if hsm_id:
            params["hsm_id"] = hsm_id
        return await self._request(
            "DELETE", self._waba_path("message_templates"), params=params
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _url(self, path: str) -> str:
        return f"{GRAPH_API_BASE_URL}/{self.api_version}/{path}"

    def _phone_path(self, suffix: str) -> str:
        if not self.phone_number_id:
            raise ValueError("This WhatsApp operation requires a phone_number_id")
        return f"{self.phone_number_id}/{suffix}"

    def _waba_path(self, suffix: str) -> str:
        if not self.waba_id:
            raise ValueError("This WhatsApp operation requires a waba_id")
        return f"{self.waba_id}/{suffix}"

    @staticmethod
    def _message_envelope(to: str) -> dict[str, Any]:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
        }

    @staticmethod
    def _media_object(
        media_id: str | None, link: str | None, *, caption: str | None = None
    ) -> dict[str, str]:
        if bool(media_id) == bool(link):
            raise ValueError("Provide exactly one of media_id or link")
        obj = {"id": media_id} if media_id else {"link": link}
        if caption:
            obj["caption"] = caption
        return obj

    async def _send_message(self, payload: dict) -> str:
        data = await self._request(
            "POST", self._phone_path("messages"), json_body=payload
        )
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError, TypeError) as e:
            raise WhatsAppApiError(
                "WhatsApp send succeeded but the response had no message id",
                retry_kind="never",
            ) from e

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict[str, str] | None = None,
        data: Any = None,
    ) -> dict:
        """Issue one Graph API request and return the parsed JSON body.

        Raises WhatsAppApiError on non-2xx responses and network failures.
        No retries — callers own the retry policy via ``retry_kind``.
        """
        url = self._url(path)
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=self._auth_headers(),
                    json=json_body,
                    params=params or None,
                    data=data,
                ) as response:
                    status = response.status
                    text = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise WhatsAppApiError(
                f"Network error calling WhatsApp API: {e}", retry_kind="backoff"
            ) from e

        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {}

        if not 200 <= status < 300:
            self._raise_api_error(status, payload, fallback_text=text)
        return payload if isinstance(payload, dict) else {}

    async def _download(self, url: str) -> tuple[bytes, str | None]:
        """GET an absolute media URL with the Bearer token; returns (bytes, content_type)."""
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    "GET", url, headers=self._auth_headers()
                ) as response:
                    status = response.status
                    body = await response.read()
                    content_type = response.headers.get("Content-Type")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise WhatsAppApiError(
                f"Network error downloading WhatsApp media: {e}", retry_kind="backoff"
            ) from e

        if not 200 <= status < 300:
            raise WhatsAppApiError(
                f"WhatsApp media download failed with HTTP {status}",
                http_status=status,
                retry_kind=_retry_kind_for(None, status),
            )
        return body, content_type

    @staticmethod
    def _raise_api_error(status: int, payload: Any, *, fallback_text: str = "") -> None:
        """Parse a Graph error body ({"error": {...}}) and raise WhatsAppApiError."""
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            error = {}
        code = error.get("code")
        subcode = error.get("error_subcode")
        message = (
            error.get("message")
            or fallback_text.strip()
            or f"WhatsApp API request failed with HTTP {status}"
        )
        raise WhatsAppApiError(
            message,
            code=code,
            subcode=subcode,
            http_status=status,
            retry_kind=_retry_kind_for(code, status),
        )


def client_for_configuration(
    config, phone_number_id: str | None = None
) -> WhatsAppClient:
    """Build a WhatsAppClient from a MessagingConfigurationModel row.

    Decrypts the stored credentials (keys: access_token, app_secret,
    verify_token, waba_id) and binds the client to ``phone_number_id``
    when given (required for message/media operations).
    """
    credentials = decrypt_credentials(config.credentials or {})
    access_token = credentials.get("access_token")
    if not access_token:
        raise ValueError(
            f"Messaging configuration {getattr(config, 'id', '?')} has no "
            "access_token credential"
        )
    return WhatsAppClient(
        access_token=access_token,
        phone_number_id=phone_number_id,
        waba_id=credentials.get("waba_id"),
    )
