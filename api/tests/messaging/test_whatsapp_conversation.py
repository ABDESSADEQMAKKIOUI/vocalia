"""Unit tests for the WhatsApp conversation runtime helpers and ARQ task glue.

Unit level only: no database, no network. DB-, Redis- and channel-touching
flows are exercised through mocks / injected fake modules.
"""

import sys
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from arq import Retry

import api.services.messaging.whatsapp.conversation_service as conversation_service
import api.tasks.whatsapp_tasks as whatsapp_tasks
from api.services.messaging.whatsapp.conversation_service import (
    STOP_KEYWORDS,
    RetryLater,
    bootstrap_whatsapp_text_session_data,
    conversation_lease_key,
    is_stop_message,
    latest_assistant_text,
    normalize_inbound_content,
    service_window_open,
)

# ----------------------------------------------------------------------
# Content normalization
# ----------------------------------------------------------------------


def test_normalize_text_message_returns_body():
    assert normalize_inbound_content("text", text="Bonjour !") == "Bonjour !"


def test_normalize_interactive_reply_prefers_title():
    assert (
        normalize_inbound_content(
            "interactive",
            interactive_reply_title="Oui, je confirme",
            interactive_reply_id="btn_confirm",
        )
        == "Oui, je confirme"
    )


def test_normalize_interactive_reply_falls_back_to_id():
    assert (
        normalize_inbound_content(
            "interactive",
            interactive_reply_title=None,
            interactive_reply_id="btn_confirm",
        )
        == "btn_confirm"
    )


def test_normalize_voice_note_returns_placeholder():
    assert (
        normalize_inbound_content("audio", is_voice_note=True)
        == "[Note vocale reçue]"
    )


def test_normalize_plain_audio_returns_placeholder():
    assert normalize_inbound_content("audio") == "[Note vocale reçue]"


def test_normalize_image_uses_caption_when_present():
    assert normalize_inbound_content("image", text="ma photo") == "ma photo"


def test_normalize_image_without_caption_returns_placeholder():
    assert normalize_inbound_content("image") == "[Image reçue]"


def test_normalize_document_without_caption_returns_placeholder():
    assert normalize_inbound_content("document") == "[Document reçu]"


def test_normalize_unsupported_type_without_text_returns_empty():
    assert normalize_inbound_content("location") == ""
    assert normalize_inbound_content("sticker") == ""
    assert normalize_inbound_content(None) == ""


def test_normalize_unknown_type_with_text_passes_it_through():
    # e.g. video captions land on `text` via the parser's media handling.
    assert normalize_inbound_content("video", text="regarde ça") == "regarde ça"


# ----------------------------------------------------------------------
# STOP keyword detection
# ----------------------------------------------------------------------


@pytest.mark.parametrize("keyword", sorted(STOP_KEYWORDS))
def test_every_stop_keyword_is_detected(keyword):
    assert is_stop_message(keyword) is True


@pytest.mark.parametrize(
    "text",
    ["STOP", "  Stop  ", "Désabonner", "ARRÊT", "\tunsubscribe\n", "Stop Promotions"],
)
def test_stop_detection_is_case_and_whitespace_insensitive(text):
    assert is_stop_message(text) is True


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "please stop",
        "stopper",
        "je veux stop les promos",
        "unstoppable",
    ],
)
def test_non_stop_messages_are_not_flagged(text):
    assert is_stop_message(text) is False


# ----------------------------------------------------------------------
# Lease key naming
# ----------------------------------------------------------------------


def test_conversation_lease_key_shape():
    assert (
        conversation_lease_key("15551884340", "33612345678")
        == "wa:conv-lease:15551884340:33612345678"
    )


# ----------------------------------------------------------------------
# User-first session bootstrap
# ----------------------------------------------------------------------


def test_bootstrap_session_data_has_no_seeded_agent_turn():
    data = bootstrap_whatsapp_text_session_data()
    assert data["turns"] == []
    assert data["status"] == "idle"
    assert data["cursor_turn_id"] is None
    assert data["discarded_future"] == []
    assert data["simulator"] == {"enabled": False, "config": {}}


def test_bootstrap_session_data_matches_text_chat_default_shape():
    # Compatibility contract: the WhatsApp bootstrap must stay in lockstep
    # with the text-chat session shape it feeds into.
    from api.services.workflow.text_chat_session_service import (
        default_text_chat_session_data,
    )

    assert bootstrap_whatsapp_text_session_data() == default_text_chat_session_data()


# ----------------------------------------------------------------------
# Reply extraction
# ----------------------------------------------------------------------


def _turn(status, text=None):
    return {
        "id": f"turn_{status}_{text}",
        "status": status,
        "assistant_message": {"text": text} if text is not None else None,
    }


def test_latest_assistant_text_reads_last_completed_turn():
    session_data = {"turns": [_turn("completed", "Bonjour"), _turn("completed", "Au revoir")]}
    assert latest_assistant_text(session_data) == "Au revoir"


def test_latest_assistant_text_ignores_earlier_turns_when_last_has_no_text():
    # Replying with an older assistant message would duplicate a send.
    session_data = {"turns": [_turn("completed", "Bonjour"), _turn("completed")]}
    assert latest_assistant_text(session_data) is None


def test_latest_assistant_text_requires_a_completed_last_turn():
    assert latest_assistant_text({"turns": [_turn("pending")]}) is None
    assert latest_assistant_text({"turns": [_turn("failed", "oops")]}) is None
    assert latest_assistant_text({"turns": []}) is None
    assert latest_assistant_text({}) is None


def test_latest_assistant_text_treats_whitespace_as_empty():
    assert latest_assistant_text({"turns": [_turn("completed", "   ")]}) is None


# ----------------------------------------------------------------------
# Service window
# ----------------------------------------------------------------------


def test_service_window_open_before_expiry():
    now = datetime.now(UTC)
    assert service_window_open(now + timedelta(hours=1), now=now) is True


def test_service_window_closed_after_expiry_or_when_unset():
    now = datetime.now(UTC)
    assert service_window_open(now - timedelta(seconds=1), now=now) is False
    assert service_window_open(None, now=now) is False


# ----------------------------------------------------------------------
# RetryLater
# ----------------------------------------------------------------------


def test_retry_later_carries_defer_seconds():
    assert RetryLater("busy").defer_seconds == 5
    assert RetryLater("backoff", defer_seconds=10).defer_seconds == 10


# ----------------------------------------------------------------------
# handle_inbound_message ordering (lease vs. wamid claim)
# ----------------------------------------------------------------------


def _inbound_msg(**overrides):
    defaults = dict(
        wamid="wamid-1",
        from_wa_id="33612345678",
        profile_name="Adil",
        phone_number_id="15551884340",
        message_type="text",
        text="Bonjour",
        interactive_reply_id=None,
        interactive_reply_title=None,
        is_voice_note=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_busy_lease_defers_without_claiming_the_wamid(monkeypatch):
    fake_redis = SimpleNamespace(
        set=AsyncMock(return_value=None),  # SET NX failed -> lease busy
        get=AsyncMock(return_value=None),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(
        conversation_service, "_get_redis", AsyncMock(return_value=fake_redis)
    )
    monkeypatch.setattr(
        conversation_service.db_client,
        "get_messaging_address_by_external_id",
        AsyncMock(return_value=SimpleNamespace(id=1, organization_id=7)),
    )
    record_wamid = AsyncMock(return_value=True)
    monkeypatch.setattr(
        conversation_service.db_client, "try_record_wamid", record_wamid
    )

    with pytest.raises(RetryLater):
        await conversation_service.handle_inbound_message(
            "15551884340", _inbound_msg()
        )

    # The wamid must not be claimed by an attempt that could not take the
    # lease — otherwise the deferred retry would skip the message entirely.
    record_wamid.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_wamid_is_skipped_and_lease_released(monkeypatch):
    fake_redis = SimpleNamespace(
        set=AsyncMock(return_value=True),
        get=AsyncMock(return_value="someone-elses-token"),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(
        conversation_service, "_get_redis", AsyncMock(return_value=fake_redis)
    )
    monkeypatch.setattr(
        conversation_service.db_client,
        "get_messaging_address_by_external_id",
        AsyncMock(return_value=SimpleNamespace(id=1, organization_id=7)),
    )
    monkeypatch.setattr(
        conversation_service.db_client,
        "try_record_wamid",
        AsyncMock(return_value=False),
    )
    process = AsyncMock()
    monkeypatch.setattr(conversation_service, "_process_inbound_message", process)

    await conversation_service.handle_inbound_message("15551884340", _inbound_msg())

    process.assert_not_awaited()
    fake_redis.get.assert_awaited_once()  # release path ran


@pytest.mark.asyncio
async def test_unknown_phone_number_id_drops_message_before_leasing(monkeypatch):
    get_redis = AsyncMock()
    monkeypatch.setattr(conversation_service, "_get_redis", get_redis)
    monkeypatch.setattr(
        conversation_service.db_client,
        "get_messaging_address_by_external_id",
        AsyncMock(return_value=None),
    )

    await conversation_service.handle_inbound_message("999", _inbound_msg())

    get_redis.assert_not_awaited()


# ----------------------------------------------------------------------
# process_whatsapp_inbound task glue
# ----------------------------------------------------------------------


def _install_fake_parser(monkeypatch, messages=(), statuses=()):
    fake_parser = types.ModuleType(
        "api.services.messaging.whatsapp.webhook_parser"
    )
    fake_parser.parse_messages_value = lambda value: (
        list(messages),
        list(statuses),
    )
    monkeypatch.setitem(
        sys.modules,
        "api.services.messaging.whatsapp.webhook_parser",
        fake_parser,
    )
    return fake_parser


@pytest.mark.asyncio
async def test_retry_later_is_converted_to_arq_retry(monkeypatch):
    _install_fake_parser(monkeypatch, messages=[_inbound_msg()])
    monkeypatch.setattr(
        whatsapp_tasks,
        "handle_inbound_message",
        AsyncMock(side_effect=RetryLater("busy", defer_seconds=7)),
    )

    with pytest.raises(Retry):
        await whatsapp_tasks.process_whatsapp_inbound(
            {}, {"kind": "messages", "phone_number_id": "15551884340", "value": {}}
        )


@pytest.mark.asyncio
async def test_one_broken_message_does_not_poison_the_batch(monkeypatch):
    _install_fake_parser(
        monkeypatch,
        messages=[_inbound_msg(wamid="w1"), _inbound_msg(wamid="w2")],
    )
    handler = AsyncMock(side_effect=[ValueError("boom"), None])
    monkeypatch.setattr(whatsapp_tasks, "handle_inbound_message", handler)

    await whatsapp_tasks.process_whatsapp_inbound(
        {}, {"kind": "messages", "phone_number_id": "15551884340", "value": {}}
    )

    assert handler.await_count == 2


@pytest.mark.asyncio
async def test_status_events_are_forwarded_to_the_campaign_dispatcher(monkeypatch):
    status = {"id": "wamid.out.1", "status": "delivered"}
    _install_fake_parser(monkeypatch, statuses=[status])
    monkeypatch.setattr(whatsapp_tasks, "handle_inbound_message", AsyncMock())

    fake_dispatcher = types.ModuleType(
        "api.services.campaign.campaign_message_dispatcher"
    )
    fake_dispatcher.handle_whatsapp_status_event = AsyncMock()
    monkeypatch.setitem(
        sys.modules,
        "api.services.campaign.campaign_message_dispatcher",
        fake_dispatcher,
    )

    await whatsapp_tasks.process_whatsapp_inbound(
        {}, {"kind": "messages", "phone_number_id": "15551884340", "value": {}}
    )

    fake_dispatcher.handle_whatsapp_status_event.assert_awaited_once_with(status)


@pytest.mark.asyncio
async def test_template_changes_are_forwarded_to_the_template_service(monkeypatch):
    fake_templates = types.ModuleType(
        "api.services.messaging.whatsapp.template_service"
    )
    fake_templates.handle_template_webhook = AsyncMock()
    monkeypatch.setitem(
        sys.modules,
        "api.services.messaging.whatsapp.template_service",
        fake_templates,
    )

    value = {"event": "APPROVED", "message_template_name": "welcome"}
    await whatsapp_tasks.process_whatsapp_inbound(
        {}, {"kind": "template_status", "phone_number_id": None, "value": value}
    )

    fake_templates.handle_template_webhook.assert_awaited_once_with(
        "template_status", value
    )


@pytest.mark.asyncio
async def test_unknown_change_kind_is_ignored():
    # Must not raise — Meta may add new change kinds at any time.
    await whatsapp_tasks.process_whatsapp_inbound(
        {}, {"kind": "something_new", "phone_number_id": None, "value": {}}
    )


# ----------------------------------------------------------------------
# sweep_whatsapp_conversations
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_closes_stale_conversations_and_enqueues_completion(monkeypatch):
    conv_closed = SimpleNamespace(id=1, workflow_run_id=11)
    conv_already_closed = SimpleNamespace(id=2, workflow_run_id=22)

    monkeypatch.setattr(
        whatsapp_tasks.db_client,
        "list_stale_whatsapp_conversations",
        AsyncMock(return_value=[conv_closed, conv_already_closed]),
    )
    monkeypatch.setattr(
        whatsapp_tasks.db_client,
        "close_whatsapp_conversation",
        AsyncMock(side_effect=[True, False]),
    )
    update_run = AsyncMock()
    monkeypatch.setattr(whatsapp_tasks.db_client, "update_workflow_run", update_run)
    monkeypatch.setattr(
        whatsapp_tasks.db_client,
        "purge_processed_wamids",
        AsyncMock(return_value=3),
    )
    monkeypatch.setattr(
        whatsapp_tasks.db_client,
        "purge_expired_messaging_suppressions",
        AsyncMock(return_value=1),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr("api.tasks.arq.enqueue_job", enqueue)

    await whatsapp_tasks.sweep_whatsapp_conversations({})

    # Only the conversation we actually transitioned to closed gets a
    # completion job; the lost-race one was already handled elsewhere.
    enqueue.assert_awaited_once_with(
        whatsapp_tasks.FunctionNames.PROCESS_WORKFLOW_COMPLETION, 11
    )
    update_run.assert_awaited_once()
    assert update_run.await_args.kwargs["run_id"] == 11
    assert update_run.await_args.kwargs["is_completed"] is True


@pytest.mark.asyncio
async def test_sweep_purges_even_when_a_close_fails(monkeypatch):
    conv = SimpleNamespace(id=1, workflow_run_id=11)
    monkeypatch.setattr(
        whatsapp_tasks.db_client,
        "list_stale_whatsapp_conversations",
        AsyncMock(return_value=[conv]),
    )
    monkeypatch.setattr(
        whatsapp_tasks.db_client,
        "close_whatsapp_conversation",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    purge_wamids = AsyncMock(return_value=0)
    purge_suppressions = AsyncMock(return_value=0)
    monkeypatch.setattr(
        whatsapp_tasks.db_client, "purge_processed_wamids", purge_wamids
    )
    monkeypatch.setattr(
        whatsapp_tasks.db_client,
        "purge_expired_messaging_suppressions",
        purge_suppressions,
    )
    monkeypatch.setattr("api.tasks.arq.enqueue_job", AsyncMock())

    await whatsapp_tasks.sweep_whatsapp_conversations({})

    purge_wamids.assert_awaited_once()
    purge_suppressions.assert_awaited_once()
