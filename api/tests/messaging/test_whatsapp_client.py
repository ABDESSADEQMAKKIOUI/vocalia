"""Unit tests for the WhatsApp Cloud API client.

Pure unit tests — no network, no database. The aiohttp session is replaced
with an in-memory fake so tests can assert the exact request method, URL,
headers and JSON bodies the client produces, and exercise the Graph error
code → retry_kind mapping.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace

import aiohttp
import pytest

from api.services.messaging.whatsapp import client as client_module
from api.services.messaging.whatsapp.client import (
    DEFAULT_API_VERSION,
    GRAPH_API_BASE_URL,
    WhatsAppApiError,
    WhatsAppClient,
    client_for_configuration,
    verify_webhook_signature,
)

APP_SECRET = "test-app-secret"
ACCESS_TOKEN = "test-access-token"
PHONE_NUMBER_ID = "111222333444555"
WABA_ID = "999888777666555"
WAMID = "wamid.HBgLMTU1NTAxMjM0NTYVAgARGBJGQkYzRjdCRkQ2Qzc2RkU2AA=="

MESSAGES_URL = f"{GRAPH_API_BASE_URL}/{DEFAULT_API_VERSION}/{PHONE_NUMBER_ID}/messages"
SEND_OK = {
    "messaging_product": "whatsapp",
    "contacts": [{"input": "15550123456", "wa_id": "15550123456"}],
    "messages": [{"id": WAMID}],
}


class FakeResponse:
    """Minimal stand-in for aiohttp's response async context manager."""

    def __init__(self, status=200, payload=None, body=b"", text=None, headers=None):
        self.status = status
        self._payload = payload
        self._body = body
        self._text = text
        self.headers = headers or {}

    async def text(self):
        if self._text is not None:
            return self._text
        if self._payload is not None:
            return json.dumps(self._payload)
        return ""

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    """Records every request; pops queued FakeResponses (or raises)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _install_session(monkeypatch, responses):
    session = FakeSession(responses)
    session_kwargs = {}

    def fake_client_session(**kwargs):
        session_kwargs.update(kwargs)
        return session

    monkeypatch.setattr(client_module.aiohttp, "ClientSession", fake_client_session)
    return session, session_kwargs


def _client(**overrides) -> WhatsAppClient:
    kwargs = {
        "access_token": ACCESS_TOKEN,
        "phone_number_id": PHONE_NUMBER_ID,
        "waba_id": WABA_ID,
    }
    kwargs.update(overrides)
    return WhatsAppClient(**kwargs)


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


def test_verify_webhook_signature_valid():
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_webhook_signature(APP_SECRET, body, _sign(body)) is True


def test_verify_webhook_signature_wrong_secret():
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_webhook_signature(APP_SECRET, body, _sign(body, "other-secret")) is False


def test_verify_webhook_signature_tampered_body():
    body = b'{"object":"whatsapp_business_account"}'
    signature = _sign(body)
    assert verify_webhook_signature(APP_SECRET, body + b"x", signature) is False


def test_verify_webhook_signature_missing_header():
    assert verify_webhook_signature(APP_SECRET, b"{}", None) is False
    assert verify_webhook_signature(APP_SECRET, b"{}", "") is False


def test_verify_webhook_signature_unprefixed_hex_rejected():
    body = b"{}"
    bare_hex = _sign(body).removeprefix("sha256=")
    assert verify_webhook_signature(APP_SECRET, body, bare_hex) is False


def test_verify_webhook_signature_missing_secret():
    body = b"{}"
    assert verify_webhook_signature("", body, _sign(body)) is False


# ---------------------------------------------------------------------------
# Send payload construction
# ---------------------------------------------------------------------------


async def test_send_text_payload(monkeypatch):
    session, session_kwargs = _install_session(monkeypatch, [FakeResponse(payload=SEND_OK)])

    wamid = await _client().send_text("15550123456", "hello there")

    assert wamid == WAMID
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == MESSAGES_URL
    assert call["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert call["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15550123456",
        "type": "text",
        "text": {"preview_url": False, "body": "hello there"},
    }
    assert session_kwargs["timeout"].total == 30


async def test_send_text_with_reply_context_and_preview(monkeypatch):
    session, _ = _install_session(monkeypatch, [FakeResponse(payload=SEND_OK)])

    await _client().send_text(
        "15550123456", "see link", preview_url=True, reply_to_wamid="wamid.reply"
    )

    body = session.calls[0]["json"]
    assert body["text"] == {"preview_url": True, "body": "see link"}
    assert body["context"] == {"message_id": "wamid.reply"}


async def test_send_template_payload(monkeypatch):
    session, _ = _install_session(monkeypatch, [FakeResponse(payload=SEND_OK)])
    components = [
        {"type": "body", "parameters": [{"type": "text", "text": "Adil"}]},
    ]

    wamid = await _client().send_template(
        "15550123456", name="order_update", language="en_US", components=components
    )

    assert wamid == WAMID
    assert session.calls[0]["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15550123456",
        "type": "template",
        "template": {
            "name": "order_update",
            "language": {"code": "en_US"},
            "components": components,
        },
    }


async def test_send_interactive_buttons_payload(monkeypatch):
    session, _ = _install_session(monkeypatch, [FakeResponse(payload=SEND_OK)])

    await _client().send_interactive_buttons(
        "15550123456",
        "Pick one",
        [{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}],
        header="Survey",
        footer="Reply anytime",
    )

    body = session.calls[0]["json"]
    assert body["type"] == "interactive"
    assert body["interactive"] == {
        "type": "button",
        "header": {"type": "text", "text": "Survey"},
        "body": {"text": "Pick one"},
        "footer": {"text": "Reply anytime"},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": "yes", "title": "Yes"}},
                {"type": "reply", "reply": {"id": "no", "title": "No"}},
            ]
        },
    }


async def test_send_interactive_buttons_validates_count():
    with pytest.raises(ValueError):
        await _client().send_interactive_buttons("1555", "body", [])
    with pytest.raises(ValueError):
        await _client().send_interactive_buttons(
            "1555", "body", [{"id": str(i), "title": str(i)} for i in range(4)]
        )


async def test_send_audio_requires_exactly_one_source():
    client = _client()
    with pytest.raises(ValueError):
        await client.send_audio("15550123456")
    with pytest.raises(ValueError):
        await client.send_audio("15550123456", media_id="123", link="https://x/a.ogg")


async def test_send_audio_and_image_payloads(monkeypatch):
    session, _ = _install_session(
        monkeypatch, [FakeResponse(payload=SEND_OK), FakeResponse(payload=SEND_OK)]
    )
    client = _client()

    await client.send_audio("15550123456", media_id="media-1")
    await client.send_image("15550123456", link="https://x/i.jpg", caption="Look")

    assert session.calls[0]["json"]["audio"] == {"id": "media-1"}
    assert session.calls[1]["json"]["image"] == {
        "link": "https://x/i.jpg",
        "caption": "Look",
    }


async def test_send_requires_phone_number_id():
    client = _client(phone_number_id=None)
    with pytest.raises(ValueError):
        await client.send_text("15550123456", "hi")


# ---------------------------------------------------------------------------
# mark_read
# ---------------------------------------------------------------------------


async def test_mark_read_payload_with_typing(monkeypatch):
    session, _ = _install_session(monkeypatch, [FakeResponse(payload={"success": True})])

    await _client().mark_read(WAMID)

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == MESSAGES_URL
    assert call["json"] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": WAMID,
        "typing_indicator": {"type": "text"},
    }


async def test_mark_read_without_typing(monkeypatch):
    session, _ = _install_session(monkeypatch, [FakeResponse(payload={"success": True})])

    await _client().mark_read(WAMID, typing=False)

    assert "typing_indicator" not in session.calls[0]["json"]


async def test_mark_read_never_raises(monkeypatch):
    error_body = {"error": {"message": "boom", "code": 100}}
    _install_session(monkeypatch, [FakeResponse(status=500, payload=error_body)])
    await _client().mark_read(WAMID)  # must not raise

    _install_session(monkeypatch, [aiohttp.ClientError("conn reset")])
    await _client().mark_read(WAMID)  # network failure must not raise either


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected_kind",
    [
        (130429, "backoff"),
        (131056, "backoff"),
        (131000, "backoff"),
        (131016, "backoff"),
        (131049, "cooldown_24h"),
        (131050, "suppress_permanent"),
        (131048, "campaign_halt"),
        (132015, "campaign_halt"),
        (132016, "campaign_halt"),
        (132000, "never"),
        (132001, "never"),
        (131026, "never"),
        (131047, "never"),
        (100, "never"),
        (424242, "never"),  # unknown code defaults to never
    ],
)
async def test_error_code_retry_kind_mapping(monkeypatch, code, expected_kind):
    error_body = {
        "error": {
            "message": "Something failed",
            "type": "OAuthException",
            "code": code,
            "error_subcode": 2494010,
        }
    }
    _install_session(monkeypatch, [FakeResponse(status=400, payload=error_body)])

    with pytest.raises(WhatsAppApiError) as exc_info:
        await _client().send_text("15550123456", "hi")

    err = exc_info.value
    assert err.retry_kind == expected_kind
    assert err.code == code
    assert err.subcode == 2494010
    assert err.http_status == 400
    assert err.message == "Something failed"


async def test_internal_5xx_without_error_json_maps_to_backoff(monkeypatch):
    _install_session(
        monkeypatch, [FakeResponse(status=503, text="Service Unavailable")]
    )

    with pytest.raises(WhatsAppApiError) as exc_info:
        await _client().send_text("15550123456", "hi")

    err = exc_info.value
    assert err.retry_kind == "backoff"
    assert err.code is None
    assert err.http_status == 503
    assert "Service Unavailable" in err.message


async def test_known_code_wins_over_5xx_status(monkeypatch):
    error_body = {"error": {"message": "opted out", "code": 131050}}
    _install_session(monkeypatch, [FakeResponse(status=500, payload=error_body)])

    with pytest.raises(WhatsAppApiError) as exc_info:
        await _client().send_text("15550123456", "hi")

    assert exc_info.value.retry_kind == "suppress_permanent"


async def test_network_error_wrapped_as_backoff(monkeypatch):
    _install_session(monkeypatch, [aiohttp.ClientError("connection reset")])

    with pytest.raises(WhatsAppApiError) as exc_info:
        await _client().send_text("15550123456", "hi")

    err = exc_info.value
    assert err.retry_kind == "backoff"
    assert err.http_status is None


async def test_send_response_without_wamid_raises(monkeypatch):
    _install_session(monkeypatch, [FakeResponse(payload={"messages": []})])

    with pytest.raises(WhatsAppApiError):
        await _client().send_text("15550123456", "hi")


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


async def test_upload_media(monkeypatch):
    session, _ = _install_session(monkeypatch, [FakeResponse(payload={"id": "media-42"})])

    media_id = await _client().upload_media(b"OggS...", "audio/ogg")

    assert media_id == "media-42"
    call = session.calls[0]
    assert call["method"] == "POST"
    assert (
        call["url"]
        == f"{GRAPH_API_BASE_URL}/{DEFAULT_API_VERSION}/{PHONE_NUMBER_ID}/media"
    )
    assert isinstance(call["data"], aiohttp.FormData)


async def test_download_media_uses_bearer_token_on_cdn_url(monkeypatch):
    media_url = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=abc"
    info = {
        "url": media_url,
        "mime_type": "audio/ogg",
        "sha256": "deadbeef",
        "file_size": 4,
        "id": "media-42",
    }
    session, _ = _install_session(
        monkeypatch,
        [
            FakeResponse(payload=info),
            FakeResponse(body=b"OggS", headers={"Content-Type": "audio/ogg"}),
        ],
    )

    data, mime_type = await _client().download_media("media-42")

    assert data == b"OggS"
    assert mime_type == "audio/ogg"
    info_call, download_call = session.calls
    assert info_call["method"] == "GET"
    assert info_call["url"] == f"{GRAPH_API_BASE_URL}/{DEFAULT_API_VERSION}/media-42"
    assert download_call["url"] == media_url
    assert download_call["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_download_media_without_url_raises(monkeypatch):
    _install_session(monkeypatch, [FakeResponse(payload={"id": "media-42"})])

    with pytest.raises(WhatsAppApiError):
        await _client().download_media("media-42")


# ---------------------------------------------------------------------------
# Template management
# ---------------------------------------------------------------------------


async def test_create_template_is_waba_scoped(monkeypatch):
    session, _ = _install_session(
        monkeypatch,
        [FakeResponse(payload={"id": "tpl-1", "status": "PENDING", "category": "MARKETING"})],
    )
    components = [{"type": "BODY", "text": "Hi {{1}}"}]

    result = await _client().create_template(
        name="welcome", language="en_US", category="MARKETING", components=components
    )

    assert result["id"] == "tpl-1"
    call = session.calls[0]
    assert call["method"] == "POST"
    assert (
        call["url"]
        == f"{GRAPH_API_BASE_URL}/{DEFAULT_API_VERSION}/{WABA_ID}/message_templates"
    )
    assert call["json"] == {
        "name": "welcome",
        "language": "en_US",
        "category": "MARKETING",
        "components": components,
        "parameter_format": "POSITIONAL",
    }


async def test_list_templates_params(monkeypatch):
    session, _ = _install_session(monkeypatch, [FakeResponse(payload={"data": []})])

    await _client().list_templates(status="APPROVED", after="cursor-1")

    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["params"] == {"status": "APPROVED", "after": "cursor-1"}


async def test_update_template_posts_to_template_id(monkeypatch):
    session, _ = _install_session(monkeypatch, [FakeResponse(payload={"success": True})])
    components = [{"type": "BODY", "text": "Hello {{1}}"}]

    await _client().update_template("tpl-123", components=components)

    call = session.calls[0]
    assert call["url"] == f"{GRAPH_API_BASE_URL}/{DEFAULT_API_VERSION}/tpl-123"
    assert call["json"] == {"components": components}


async def test_update_template_requires_changes():
    with pytest.raises(ValueError):
        await _client().update_template("tpl-123")


async def test_delete_template_params(monkeypatch):
    session, _ = _install_session(monkeypatch, [FakeResponse(payload={"success": True})])

    await _client().delete_template(name="welcome", hsm_id="tpl-123")

    call = session.calls[0]
    assert call["method"] == "DELETE"
    assert (
        call["url"]
        == f"{GRAPH_API_BASE_URL}/{DEFAULT_API_VERSION}/{WABA_ID}/message_templates"
    )
    assert call["params"] == {"name": "welcome", "hsm_id": "tpl-123"}


async def test_template_operations_require_waba_id():
    client = _client(waba_id=None)
    with pytest.raises(ValueError):
        await client.list_templates()


# ---------------------------------------------------------------------------
# client_for_configuration
# ---------------------------------------------------------------------------


def test_client_for_configuration_decrypts_credentials(monkeypatch):
    decrypted = {
        "access_token": ACCESS_TOKEN,
        "app_secret": APP_SECRET,
        "verify_token": "verify-me",
        "waba_id": WABA_ID,
    }
    seen = {}

    def fake_decrypt(credentials):
        seen["credentials"] = credentials
        return decrypted

    monkeypatch.setattr(client_module, "decrypt_credentials", fake_decrypt)
    config = SimpleNamespace(id=7, credentials={"access_token": "enc:v1:xyz"})

    client = client_for_configuration(config, phone_number_id=PHONE_NUMBER_ID)

    assert seen["credentials"] == {"access_token": "enc:v1:xyz"}
    assert client.access_token == ACCESS_TOKEN
    assert client.phone_number_id == PHONE_NUMBER_ID
    assert client.waba_id == WABA_ID
    assert client.api_version == DEFAULT_API_VERSION


def test_client_for_configuration_missing_access_token(monkeypatch):
    monkeypatch.setattr(client_module, "decrypt_credentials", lambda c: {})
    config = SimpleNamespace(id=7, credentials={})

    with pytest.raises(ValueError):
        client_for_configuration(config)
