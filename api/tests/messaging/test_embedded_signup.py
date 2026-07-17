"""Unit tests for WhatsApp Embedded Signup.

Pure unit tests — no network, no database. Covers:
  - ``embedded_signup_public_config`` / ``is_embedded_signup_enabled`` toggled
    by the deployment env vars (monkeypatched).
  - ``EmbeddedSignupCompleteRequest`` schema validation.
  - ``WhatsAppClient.exchange_oauth_code`` request construction, via the same
    in-memory fake aiohttp session used by ``test_whatsapp_client.py``.
"""

import json

import pytest
from pydantic import ValidationError

from api.schemas.messaging_config import EmbeddedSignupCompleteRequest
from api.services.messaging.whatsapp import client as client_module
from api.services.messaging.whatsapp.client import (
    GRAPH_API_BASE_URL,
    WhatsAppApiError,
    WhatsAppClient,
)
from api.services.messaging.whatsapp.embedded_signup import (
    embedded_signup_public_config,
    is_embedded_signup_enabled,
)

_ENV_VARS = (
    "WHATSAPP_APP_ID",
    "WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID",
    "WHATSAPP_GRAPH_VERSION",
)


# ---------------------------------------------------------------------------
# Fake aiohttp session (mirrors test_whatsapp_client.py)
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for aiohttp's response async context manager."""

    def __init__(self, status=200, payload=None, text=None):
        self.status = status
        self._payload = payload
        self._text = text
        self.headers = {}

    async def text(self):
        if self._text is not None:
            return self._text
        if self._payload is not None:
            return json.dumps(self._payload)
        return ""

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


def _clear_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# embedded_signup_public_config / is_embedded_signup_enabled
# ---------------------------------------------------------------------------


def test_public_config_disabled_when_env_missing(monkeypatch):
    _clear_env(monkeypatch)

    config = embedded_signup_public_config()

    assert config == {
        "enabled": False,
        "app_id": None,
        "config_id": None,
        "graph_version": "v23.0",
    }
    assert is_embedded_signup_enabled() is False


def test_public_config_disabled_with_only_app_id(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_APP_ID", "app-123")

    config = embedded_signup_public_config()

    assert config["enabled"] is False
    assert config["app_id"] == "app-123"
    assert config["config_id"] is None
    assert is_embedded_signup_enabled() is False


def test_public_config_disabled_with_only_config_id(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID", "cfg-456")

    assert embedded_signup_public_config()["enabled"] is False
    assert is_embedded_signup_enabled() is False


def test_public_config_enabled_when_both_set(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_APP_ID", "app-123")
    monkeypatch.setenv("WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID", "cfg-456")
    monkeypatch.setenv("WHATSAPP_GRAPH_VERSION", "v21.0")

    config = embedded_signup_public_config()

    assert config == {
        "enabled": True,
        "app_id": "app-123",
        "config_id": "cfg-456",
        "graph_version": "v21.0",
    }
    assert is_embedded_signup_enabled() is True


def test_public_config_defaults_graph_version(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_APP_ID", "app-123")
    monkeypatch.setenv("WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID", "cfg-456")

    assert embedded_signup_public_config()["graph_version"] == "v23.0"


# ---------------------------------------------------------------------------
# EmbeddedSignupCompleteRequest schema validation
# ---------------------------------------------------------------------------


def test_complete_request_requires_code():
    with pytest.raises(ValidationError):
        EmbeddedSignupCompleteRequest(waba_id="w", phone_number_id="p")


def test_complete_request_rejects_empty_code():
    with pytest.raises(ValidationError):
        EmbeddedSignupCompleteRequest(code="", waba_id="w", phone_number_id="p")


def test_complete_request_requires_waba_and_phone():
    with pytest.raises(ValidationError):
        EmbeddedSignupCompleteRequest(code="c", phone_number_id="p")
    with pytest.raises(ValidationError):
        EmbeddedSignupCompleteRequest(code="c", waba_id="w")


def test_complete_request_optional_fields_default_none():
    req = EmbeddedSignupCompleteRequest(
        code="c", waba_id="w", phone_number_id="p"
    )

    assert req.business_id is None
    assert req.name is None


def test_complete_request_accepts_optional_fields():
    req = EmbeddedSignupCompleteRequest(
        code="c",
        waba_id="w",
        phone_number_id="p",
        business_id="biz-1",
        name="Support line",
    )

    assert req.business_id == "biz-1"
    assert req.name == "Support line"


# ---------------------------------------------------------------------------
# WhatsAppClient.exchange_oauth_code
# ---------------------------------------------------------------------------


async def test_exchange_oauth_code_builds_request(monkeypatch):
    session, session_kwargs = _install_session(
        monkeypatch,
        [FakeResponse(payload={"access_token": "long-lived-token"})],
    )

    token = await WhatsAppClient.exchange_oauth_code(
        "app-123", "secret-xyz", "code-abc", graph_version="v23.0"
    )

    assert token == "long-lived-token"
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{GRAPH_API_BASE_URL}/v23.0/oauth/access_token"
    assert call["params"] == {
        "client_id": "app-123",
        "client_secret": "secret-xyz",
        "code": "code-abc",
    }
    # The app credentials travel as params — no bearer token is sent.
    assert "headers" not in call or "Authorization" not in call.get("headers", {})
    assert session_kwargs["timeout"].total == 30


async def test_exchange_oauth_code_honors_graph_version(monkeypatch):
    session, _ = _install_session(
        monkeypatch, [FakeResponse(payload={"access_token": "t"})]
    )

    await WhatsAppClient.exchange_oauth_code(
        "app", "secret", "code", graph_version="v19.0"
    )

    assert session.calls[0]["url"] == (
        f"{GRAPH_API_BASE_URL}/v19.0/oauth/access_token"
    )


async def test_exchange_oauth_code_raises_on_error(monkeypatch):
    error_body = {"error": {"message": "Invalid verification code.", "code": 100}}
    _install_session(monkeypatch, [FakeResponse(status=400, payload=error_body)])

    with pytest.raises(WhatsAppApiError) as exc_info:
        await WhatsAppClient.exchange_oauth_code(
            "app", "secret", "bad-code", graph_version="v23.0"
        )

    err = exc_info.value
    assert err.http_status == 400
    assert err.code == 100
    assert "Invalid verification code." in err.message


async def test_exchange_oauth_code_missing_token_raises(monkeypatch):
    _install_session(monkeypatch, [FakeResponse(payload={"token_type": "bearer"})])

    with pytest.raises(WhatsAppApiError):
        await WhatsAppClient.exchange_oauth_code(
            "app", "secret", "code", graph_version="v23.0"
        )
