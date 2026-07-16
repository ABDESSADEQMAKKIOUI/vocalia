"""Unit tests for WhatsApp webhook payload parsing, signature verification,
and the public webhook route handlers.

Pure tests: no network, no DB. Route tests build starlette Requests directly
(same pattern as api/tests/telephony/twilio/test_routes.py) and mock ARQ /
db_client at the route module boundary.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

from api.services.messaging.whatsapp.webhook_parser import (
    ParsedInboundMessage,
    parse_messages_value,
    parse_webhook_payload,
)

APP_SECRET = "synthetic-app-secret"

PHONE_NUMBER_ID = "106540352242922"
DISPLAY_PHONE_NUMBER = "15550001111"
USER_WA_ID = "16505551234"


# ---------------------------------------------------------------------------
# Payload fixtures
# ---------------------------------------------------------------------------
def _messages_payload(value_overrides: dict) -> dict:
    """A full webhook payload wrapping one "messages" change."""
    value = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": DISPLAY_PHONE_NUMBER,
            "phone_number_id": PHONE_NUMBER_ID,
        },
        "contacts": [{"profile": {"name": "Kerry Fisher"}, "wa_id": USER_WA_ID}],
        **value_overrides,
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "102290129340398",
                "changes": [{"field": "messages", "value": value}],
            }
        ],
    }


def _text_message(body: str, **extra) -> dict:
    return {
        "from": USER_WA_ID,
        "id": "wamid.text.1",
        "timestamp": "1720000000",
        "type": "text",
        "text": {"body": body},
        **extra,
    }


def _sign(raw_body: bytes, secret: str = APP_SECRET) -> str:
    return (
        "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    )


def _request(
    *,
    method: str = "POST",
    path: str = "/api/v1/messaging/whatsapp/webhook",
    query: dict[str, str] | None = None,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> Request:
    request_headers = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "server": ("example.test", 443),
            "path": path,
            "query_string": urlencode(query or {}).encode("utf-8"),
            "headers": request_headers,
        },
        receive,
    )


# ---------------------------------------------------------------------------
# Parser: messages
# ---------------------------------------------------------------------------
def test_parse_text_message_with_quoted_reply():
    payload = _messages_payload(
        {
            "messages": [
                _text_message(
                    "Yes, book it",
                    context={"from": DISPLAY_PHONE_NUMBER, "id": "wamid.quoted.7"},
                )
            ]
        }
    )

    changes = parse_webhook_payload(payload)
    assert len(changes) == 1
    assert changes[0]["kind"] == "messages"
    assert changes[0]["phone_number_id"] == PHONE_NUMBER_ID

    messages, statuses = parse_messages_value(changes[0]["value"])
    assert statuses == []
    assert len(messages) == 1
    message = messages[0]
    assert message.wamid == "wamid.text.1"
    assert message.from_wa_id == USER_WA_ID
    assert message.profile_name == "Kerry Fisher"
    assert message.phone_number_id == PHONE_NUMBER_ID
    assert message.display_phone_number == DISPLAY_PHONE_NUMBER
    assert message.message_type == "text"
    assert message.text == "Yes, book it"
    assert message.quoted_wamid == "wamid.quoted.7"
    assert message.timestamp == 1720000000
    assert message.is_voice_note is False
    assert message.media_id is None
    assert message.raw["text"]["body"] == "Yes, book it"


def test_parse_voice_note():
    payload = _messages_payload(
        {
            "messages": [
                {
                    "from": USER_WA_ID,
                    "id": "wamid.audio.1",
                    "timestamp": "1720000001",
                    "type": "audio",
                    "audio": {
                        "id": "media-id-123",
                        "mime_type": "audio/ogg; codecs=opus",
                        "sha256": "abc=",
                        "voice": True,
                    },
                }
            ]
        }
    )

    messages, _ = parse_messages_value(parse_webhook_payload(payload)[0]["value"])
    message = messages[0]
    assert message.message_type == "audio"
    assert message.media_id == "media-id-123"
    assert message.mime_type == "audio/ogg; codecs=opus"
    assert message.is_voice_note is True
    assert message.text is None


def test_parse_interactive_button_reply():
    payload = _messages_payload(
        {
            "messages": [
                {
                    "from": USER_WA_ID,
                    "id": "wamid.interactive.1",
                    "timestamp": "1720000002",
                    "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {"id": "confirm_yes", "title": "Yes"},
                    },
                }
            ]
        }
    )

    messages, _ = parse_messages_value(parse_webhook_payload(payload)[0]["value"])
    message = messages[0]
    assert message.message_type == "interactive"
    assert message.interactive_reply_id == "confirm_yes"
    assert message.interactive_reply_title == "Yes"
    assert message.text == "Yes"


def test_parse_statuses_batch():
    payload = _messages_payload(
        {
            "contacts": [],
            "statuses": [
                {
                    "id": "wamid.out.1",
                    "status": "delivered",
                    "timestamp": "1720000010",
                    "recipient_id": USER_WA_ID,
                    "conversation": {"id": "conv-1"},
                },
                {
                    "id": "wamid.out.2",
                    "status": "failed",
                    "timestamp": "1720000011",
                    "recipient_id": USER_WA_ID,
                    "errors": [
                        {
                            "code": 131047,
                            "title": "Re-engagement message",
                            "error_data": {
                                "details": "Message failed to send because more "
                                "than 24 hours have passed"
                            },
                        }
                    ],
                },
            ],
        }
    )

    messages, statuses = parse_messages_value(
        parse_webhook_payload(payload)[0]["value"]
    )
    assert messages == []
    assert len(statuses) == 2
    assert statuses[0]["status"] == "delivered"
    assert statuses[1]["status"] == "failed"
    assert statuses[1]["errors"][0]["code"] == 131047


# ---------------------------------------------------------------------------
# Parser: template events and defensiveness
# ---------------------------------------------------------------------------
def test_parse_template_status_update():
    value = {
        "event": "APPROVED",
        "message_template_id": 1234567,
        "message_template_name": "order_update",
        "message_template_language": "en_US",
        "reason": None,
    }
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {"field": "message_template_status_update", "value": value}
                ],
            }
        ],
    }

    changes = parse_webhook_payload(payload)
    assert len(changes) == 1
    assert changes[0]["kind"] == "template_status"
    assert changes[0]["phone_number_id"] is None
    assert changes[0]["value"] == value


def test_unknown_change_fields_are_skipped():
    payload = {
        "entry": [
            {"changes": [{"field": "account_update", "value": {"event": "x"}}]}
        ]
    }
    assert parse_webhook_payload(payload) == []


def test_malformed_payloads_never_raise():
    assert parse_webhook_payload({}) == []
    assert parse_webhook_payload({"entry": "not-a-list"}) == []
    assert parse_webhook_payload({"entry": [None, 42, {"changes": None}]}) == []

    # A change with no value still produces a work item.
    changes = parse_webhook_payload(
        {"entry": [{"changes": [{"field": "messages"}]}]}
    )
    assert changes == [{"kind": "messages", "phone_number_id": None, "value": {}}]

    messages, statuses = parse_messages_value({})
    assert messages == [] and statuses == []

    # Unknown message type, no contacts, no type-specific content.
    messages, _ = parse_messages_value(
        {"messages": [{"id": "wamid.x", "from": USER_WA_ID, "type": "order"}]}
    )
    assert messages[0].message_type == "order"
    assert messages[0].text is None
    assert messages[0].profile_name is None
    assert messages[0].timestamp is None


def test_parsed_message_round_trips_through_json():
    payload = _messages_payload({"messages": [_text_message("hello")]})
    message = parse_messages_value(parse_webhook_payload(payload)[0]["value"])[0][0]

    rehydrated = ParsedInboundMessage.from_dict(
        json.loads(json.dumps(message.to_dict()))
    )
    assert rehydrated == message

    # from_dict tolerates partial/extra keys.
    partial = ParsedInboundMessage.from_dict({"wamid": "w1", "unknown_key": 1})
    assert partial.wamid == "w1"
    assert partial.message_type == "unknown"


# ---------------------------------------------------------------------------
# Signature verification (shared helper from the client module)
# ---------------------------------------------------------------------------
def test_verify_webhook_signature_accepts_valid_signature():
    client = pytest.importorskip("api.services.messaging.whatsapp.client")
    raw_body = json.dumps(_messages_payload({"messages": []})).encode()

    assert client.verify_webhook_signature(APP_SECRET, raw_body, _sign(raw_body))


def test_verify_webhook_signature_rejects_invalid_signatures():
    client = pytest.importorskip("api.services.messaging.whatsapp.client")
    raw_body = b'{"object": "whatsapp_business_account", "entry": []}'

    tampered = raw_body + b" "
    assert not client.verify_webhook_signature(
        APP_SECRET, tampered, _sign(raw_body)
    )
    assert not client.verify_webhook_signature(
        APP_SECRET, raw_body, "sha256=" + "0" * 64
    )
    assert not client.verify_webhook_signature(APP_SECRET, raw_body, None)
    assert not client.verify_webhook_signature(
        "a-different-secret", raw_body, _sign(raw_body)
    )


# ---------------------------------------------------------------------------
# Route: GET handshake
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_webhook_echoes_challenge_on_token_match(monkeypatch):
    pytest.importorskip("api.services.messaging.whatsapp.client")
    from api.routes.messaging_whatsapp import verify_whatsapp_webhook

    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    request = _request(
        method="GET",
        query={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "1158201444",
        },
    )

    response = await verify_whatsapp_webhook(request)
    assert response.status_code == 200
    assert response.body == b"1158201444"


@pytest.mark.asyncio
async def test_get_webhook_rejects_bad_or_unconfigured_token(monkeypatch):
    pytest.importorskip("api.services.messaging.whatsapp.client")
    from api.routes.messaging_whatsapp import verify_whatsapp_webhook

    query = {
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong",
        "hub.challenge": "123",
    }

    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    response = await verify_whatsapp_webhook(_request(method="GET", query=query))
    assert response.status_code == 403

    # No deployment token configured -> always 403.
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN", raising=False)
    query["hub.verify_token"] = "verify-me"
    response = await verify_whatsapp_webhook(_request(method="GET", query=query))
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Route: POST delivery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_post_webhook_verifies_env_secret_and_enqueues_changes(monkeypatch):
    pytest.importorskip("api.services.messaging.whatsapp.client")
    from api.routes.messaging_whatsapp import receive_whatsapp_webhook

    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    raw_body = json.dumps(
        _messages_payload({"messages": [_text_message("hi")]})
    ).encode()
    request = _request(
        body=raw_body, headers={"X-Hub-Signature-256": _sign(raw_body)}
    )

    with patch(
        "api.routes.messaging_whatsapp.enqueue_job", new_callable=AsyncMock
    ) as mock_enqueue:
        result = await receive_whatsapp_webhook(request)

    assert result == {"status": "ok"}
    assert mock_enqueue.await_count == 1
    function_name, change = mock_enqueue.await_args.args
    assert function_name == "process_whatsapp_inbound"
    assert change["kind"] == "messages"
    assert change["phone_number_id"] == PHONE_NUMBER_ID


@pytest.mark.asyncio
async def test_post_webhook_rejects_invalid_signature(monkeypatch):
    pytest.importorskip("api.services.messaging.whatsapp.client")
    from api.routes.messaging_whatsapp import receive_whatsapp_webhook

    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    raw_body = json.dumps(_messages_payload({"messages": []})).encode()

    for signature in (_sign(raw_body + b"tamper"), "sha256=bad", None):
        headers = (
            {"X-Hub-Signature-256": signature} if signature is not None else {}
        )
        request = _request(body=raw_body, headers=headers)
        with patch(
            "api.routes.messaging_whatsapp.enqueue_job", new_callable=AsyncMock
        ) as mock_enqueue:
            response = await receive_whatsapp_webhook(request)
        assert response.status_code == 403
        mock_enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_webhook_falls_back_to_configuration_app_secret(monkeypatch):
    pytest.importorskip("api.services.messaging.whatsapp.client")
    from api.routes.messaging_whatsapp import receive_whatsapp_webhook

    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    raw_body = json.dumps(
        _messages_payload({"messages": [_text_message("hi")]})
    ).encode()
    request = _request(
        body=raw_body, headers={"X-Hub-Signature-256": _sign(raw_body)}
    )
    # Plaintext credential values pass through decrypt_credentials untouched,
    # so no encryption key is needed here.
    address = SimpleNamespace(
        messaging_configuration_id=7,
        configuration=SimpleNamespace(credentials={"app_secret": APP_SECRET}),
    )

    with (
        patch(
            "api.routes.messaging_whatsapp.db_client.get_messaging_address_by_external_id",
            new_callable=AsyncMock,
            return_value=address,
        ) as mock_lookup,
        patch(
            "api.routes.messaging_whatsapp.enqueue_job", new_callable=AsyncMock
        ) as mock_enqueue,
    ):
        result = await receive_whatsapp_webhook(request)

    assert result == {"status": "ok"}
    mock_lookup.assert_awaited_once_with(PHONE_NUMBER_ID)
    assert mock_enqueue.await_count == 1


@pytest.mark.asyncio
async def test_post_webhook_returns_ok_even_when_enqueue_fails(monkeypatch):
    pytest.importorskip("api.services.messaging.whatsapp.client")
    from api.routes.messaging_whatsapp import receive_whatsapp_webhook

    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    raw_body = json.dumps(
        _messages_payload({"messages": [_text_message("hi")]})
    ).encode()
    request = _request(
        body=raw_body, headers={"X-Hub-Signature-256": _sign(raw_body)}
    )

    with patch(
        "api.routes.messaging_whatsapp.enqueue_job",
        new_callable=AsyncMock,
        side_effect=ConnectionError("redis down"),
    ):
        result = await receive_whatsapp_webhook(request)

    assert result == {"status": "ok"}
