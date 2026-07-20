"""Unit tests for the WhatsApp inbox service helpers and schemas.

Pure level only: no database, no network. Thread building, the 24h
service-window check and the response schemas are exercised with plain
dicts / SimpleNamespace stand-ins for ORM rows.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from api.schemas.messaging_inbox import (
    WhatsAppAgentPausedResponse,
    WhatsAppConversationDetailResponse,
    WhatsAppConversationListResponse,
    WhatsAppConversationSummary,
    WhatsAppHumanReplyRequest,
    WhatsAppInboxMessage,
)
from api.services.messaging.whatsapp.inbox_service import (
    TEMPLATE_SENT_FALLBACK_LABEL,
    build_human_reply_turn,
    build_thread,
    human_reply_window_open,
    last_message_preview,
    truncate_preview,
)

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def _session(turns):
    return SimpleNamespace(session_data={"turns": turns})


def _turn(
    *,
    user_text=None,
    assistant_text=None,
    status="completed",
    origin=None,
    user_at="2026-07-18T10:00:00+00:00",
    assistant_at="2026-07-18T10:00:05+00:00",
):
    turn = {
        "id": "turn_abc",
        "status": status,
        "created_at": user_at,
        "user_message": (
            {"text": user_text, "created_at": user_at} if user_text else None
        ),
        "assistant_message": (
            {"text": assistant_text, "created_at": assistant_at}
            if assistant_text
            else None
        ),
        "events": [],
        "usage": {},
    }
    if origin is not None:
        turn["origin"] = origin
    return turn


# ----------------------------------------------------------------------
# build_thread
# ----------------------------------------------------------------------


def test_build_thread_empty_session_returns_no_messages():
    assert build_thread(_session([])) == []
    assert build_thread(None) == []
    assert build_thread(SimpleNamespace(session_data=None)) == []


def test_build_thread_user_first_exchange_is_chronological():
    session = _session(
        [_turn(user_text="Bonjour", assistant_text="Bonjour ! Comment aider ?")]
    )
    messages = build_thread(session)
    assert [(m["direction"], m["origin"], m["text"]) for m in messages] == [
        ("in", "user", "Bonjour"),
        ("out", "agent", "Bonjour ! Comment aider ?"),
    ]
    assert messages[0]["timestamp"] == "2026-07-18T10:00:00+00:00"
    assert messages[1]["timestamp"] == "2026-07-18T10:00:05+00:00"


def test_build_thread_agent_greeting_turn_without_user_message():
    # Browser-tester style seeded turn: assistant only, no user message.
    session = _session([_turn(assistant_text="Bienvenue chez Dograh !")])
    messages = build_thread(session)
    assert messages == [
        {
            "direction": "out",
            "origin": "agent",
            "text": "Bienvenue chez Dograh !",
            "timestamp": "2026-07-18T10:00:05+00:00",
        }
    ]


def test_build_thread_human_origin_turn_is_flagged_human():
    session = _session(
        [
            _turn(user_text="Je veux parler à un humain", assistant_text="Bien sûr."),
            _turn(assistant_text="C'est Alice, je prends le relais.", origin="human"),
        ]
    )
    messages = build_thread(session)
    assert [m["origin"] for m in messages] == ["user", "agent", "human"]
    assert messages[-1]["direction"] == "out"


def test_build_thread_skips_empty_and_failed_turn_content():
    session = _session(
        [
            _turn(user_text="Allo ?", assistant_text=None, status="failed"),
            _turn(user_text="  ", assistant_text="   "),
        ]
    )
    messages = build_thread(session)
    assert [(m["direction"], m["text"]) for m in messages] == [("in", "Allo ?")]


def test_build_thread_prepends_campaign_template_entry():
    created_at = NOW - timedelta(hours=2)
    conversation = SimpleNamespace(
        campaign_id=7, created_at=created_at, last_outbound_at=NOW
    )
    run = SimpleNamespace(
        gathered_context={"call_id": "wamid.X", "provider": "whatsapp"}, logs=None
    )
    session = _session([_turn(user_text="Oui, intéressé")])

    messages = build_thread(
        session, conversation, run, template_name="relance_juillet"
    )
    assert messages[0] == {
        "direction": "out",
        "origin": "template",
        "text": "[Message modèle: relance_juillet]",
        "timestamp": created_at.isoformat(),
    }
    assert messages[1]["origin"] == "user"


def test_build_thread_template_entry_falls_back_without_name():
    conversation = SimpleNamespace(
        campaign_id=7, created_at=NOW, last_outbound_at=None
    )
    run = SimpleNamespace(
        gathered_context={},
        logs={"whatsapp_status_callbacks": [{"status": "delivered"}]},
    )
    messages = build_thread(None, conversation, run)
    assert messages == [
        {
            "direction": "out",
            "origin": "template",
            "text": TEMPLATE_SENT_FALLBACK_LABEL,
            "timestamp": NOW.isoformat(),
        }
    ]


def test_build_thread_no_template_entry_without_send_evidence():
    conversation = SimpleNamespace(
        campaign_id=7, created_at=NOW, last_outbound_at=None
    )
    run = SimpleNamespace(gathered_context={}, logs={})
    assert build_thread(None, conversation, run) == []
    # Organic (non-campaign) conversations never get a template entry.
    organic = SimpleNamespace(campaign_id=None, created_at=NOW, last_outbound_at=NOW)
    sent_run = SimpleNamespace(
        gathered_context={"call_id": "wamid.X", "provider": "whatsapp"}, logs=None
    )
    assert build_thread(None, organic, sent_run) == []


# ----------------------------------------------------------------------
# Service-window check
# ----------------------------------------------------------------------


def test_window_open_when_stored_expiry_is_in_the_future():
    assert human_reply_window_open(
        service_window_expires_at=NOW + timedelta(minutes=1),
        last_inbound_at=None,
        now=NOW,
    )


def test_window_closed_when_stored_expiry_passed_and_no_recent_inbound():
    assert not human_reply_window_open(
        service_window_expires_at=NOW - timedelta(seconds=1),
        last_inbound_at=NOW - timedelta(hours=25),
        now=NOW,
    )


def test_window_open_from_recent_inbound_without_stored_expiry():
    assert human_reply_window_open(
        service_window_expires_at=None,
        last_inbound_at=NOW - timedelta(hours=23, minutes=59),
        now=NOW,
    )


def test_window_closed_when_nothing_recorded():
    assert not human_reply_window_open(
        service_window_expires_at=None, last_inbound_at=None, now=NOW
    )


def test_window_closed_exactly_24h_after_inbound():
    assert not human_reply_window_open(
        service_window_expires_at=None,
        last_inbound_at=NOW - timedelta(hours=24),
        now=NOW,
    )


# ----------------------------------------------------------------------
# Previews and human-reply turn shape
# ----------------------------------------------------------------------


def test_truncate_preview_caps_at_80_chars_and_collapses_whitespace():
    assert truncate_preview("court") == "court"
    assert truncate_preview("un   mot\n à la  fois") == "un mot à la fois"
    long_text = "x" * 200
    preview = truncate_preview(long_text)
    assert len(preview) == 80
    assert preview.endswith("…")
    assert truncate_preview(None) is None
    assert truncate_preview("   ") is None


def test_last_message_preview_prefers_latest_message():
    session_data = {
        "turns": [
            _turn(user_text="Bonjour", assistant_text="Salut !"),
            _turn(user_text="Sans réponse encore", assistant_text=None),
        ]
    }
    assert last_message_preview(session_data) == "Sans réponse encore"
    assert last_message_preview({"turns": []}) is None
    assert last_message_preview(None) is None


def test_build_human_reply_turn_mirrors_runtime_turn_shape():
    turn = build_human_reply_turn("On s'en occupe !", now_iso="2026-07-18T12:00:00+00:00")
    assert turn["status"] == "completed"
    assert turn["origin"] == "human"
    assert turn["user_message"] is None
    assert turn["assistant_message"] == {
        "text": "On s'en occupe !",
        "created_at": "2026-07-18T12:00:00+00:00",
    }
    assert turn["id"].startswith("turn_")
    assert turn["events"] == [] and turn["usage"] == {}


# ----------------------------------------------------------------------
# Schemas (pinned HTTP contract shapes)
# ----------------------------------------------------------------------


def _summary_payload(**overrides):
    payload = {
        "id": 1,
        "wa_id": "33612345678",
        "profile_name": "Adil",
        "phone_number_id": "1029384756",
        "display_number": "+33 1 99 00 11 22",
        "state": "open",
        "agent_paused": False,
        "service_window_expires_at": NOW + timedelta(hours=3),
        "last_inbound_at": NOW - timedelta(hours=21),
        "last_outbound_at": NOW - timedelta(hours=20),
        "workflow_run_id": 42,
        "campaign_id": None,
        "agent_name": "Agent Relance",
        "updated_at": NOW,
        "last_message_preview": "Bonjour !",
    }
    payload.update(overrides)
    return payload


def test_conversation_summary_accepts_nullable_fields():
    summary = WhatsAppConversationSummary(
        **_summary_payload(
            profile_name=None,
            display_number=None,
            service_window_expires_at=None,
            last_inbound_at=None,
            last_outbound_at=None,
            agent_name=None,
            updated_at=None,
            last_message_preview=None,
        )
    )
    assert summary.agent_name is None
    assert summary.last_message_preview is None


def test_list_and_detail_responses_round_trip():
    detail = WhatsAppConversationDetailResponse(
        conversation=_summary_payload(),
        messages=[
            {"direction": "in", "origin": "user", "text": "Bonjour", "timestamp": None},
            {
                "direction": "out",
                "origin": "human",
                "text": "Bonjour, ici Alice",
                "timestamp": "2026-07-18T10:00:05+00:00",
            },
        ],
    )
    assert detail.messages[1].origin == "human"
    listing = WhatsAppConversationListResponse(conversations=[_summary_payload()])
    assert listing.conversations[0].id == 1


def test_inbox_message_rejects_unknown_direction_and_origin():
    with pytest.raises(ValidationError):
        WhatsAppInboxMessage(direction="sideways", origin="user", text="x")
    with pytest.raises(ValidationError):
        WhatsAppInboxMessage(direction="in", origin="robot", text="x")


def test_human_reply_request_requires_non_empty_bounded_text():
    assert WhatsAppHumanReplyRequest(text="ok").text == "ok"
    with pytest.raises(ValidationError):
        WhatsAppHumanReplyRequest(text="")
    with pytest.raises(ValidationError):
        WhatsAppHumanReplyRequest(text="x" * 5000)


def test_agent_paused_response_shape():
    payload = WhatsAppAgentPausedResponse(id=3, agent_paused=True).model_dump()
    assert payload == {"id": 3, "agent_paused": True}


def test_build_thread_template_entry_prefers_rendered_text():
    """The bubble shows the delivered message text, not the template name."""
    created_at = NOW - timedelta(hours=2)
    conversation = SimpleNamespace(
        campaign_id=7, created_at=created_at, last_outbound_at=NOW
    )
    run = SimpleNamespace(
        gathered_context={"call_id": "wamid.X", "provider": "whatsapp"}, logs=None
    )
    session = _session([])
    messages = build_thread(
        session,
        conversation,
        run,
        template_name="hello_world",
        template_text="Hello World\nWelcome to our service!",
    )
    assert messages[0]["origin"] == "template"
    assert messages[0]["text"] == "Hello World\nWelcome to our service!"
    assert "hello_world" not in messages[0]["text"]


def test_render_template_display_text_substitutes_and_joins():
    from api.services.messaging.whatsapp.template_service import (
        render_template_display_text,
    )

    components = [
        {"type": "HEADER", "format": "TEXT", "text": "Bonjour {{1}}"},
        {"type": "BODY", "text": "Votre commande {{2}} est prête."},
        {"type": "FOOTER", "text": "YAMED"},
        {"type": "BUTTONS", "buttons": [
            {"type": "QUICK_REPLY", "text": "Confirmer"},
            {"type": "URL", "text": "Suivre", "url": "https://x.co/{{1}}"},
        ]},
    ]
    text = render_template_display_text(
        components, "positional", {"1": "Youssef", "2": "A-42"}
    )
    assert text == (
        "Bonjour Youssef\nVotre commande A-42 est prête.\nYAMED\n[Confirmer] · [Suivre]"
    )


def test_render_template_display_text_missing_value_left_verbatim():
    from api.services.messaging.whatsapp.template_service import (
        render_template_display_text,
    )

    text = render_template_display_text(
        [{"type": "BODY", "text": "Salut {{prenom}} !"}], "named", {}
    )
    assert text == "Salut {{prenom}} !"
