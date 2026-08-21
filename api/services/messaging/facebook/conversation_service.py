"""Facebook Messenger conversation runtime: inbound -> agent turn -> reply.

Mirrors the WhatsApp conversation runtime, but keeps per-conversation state
(the open workflow run + message dedup) in Redis instead of dedicated SQL
tables, so no schema migration is needed. One Messenger conversation —
``(page_id, psid)`` within the 24h standard messaging window — maps to one
workflow run (mode=facebook) whose assistant turns are produced by the shared
text-chat session machinery (the same runner WhatsApp and the web tester use).

Concurrency: processing is serialized per ``(page_id, psid)`` via a short
Redis lease; a busy lease or a transient Send API failure raises RetryLater,
which the ARQ task converts into an ``arq.Retry`` deferral. Inbound messages
are deduped on their ``mid`` so a Meta redelivery is a no-op.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
from loguru import logger

from api.constants import REDIS_URL
from api.db import db_client
from api.enums import CallType, WorkflowRunMode, WorkflowRunState

CONVERSATION_LEASE_TTL_SECONDS = 180
LEASE_BUSY_DEFER_SECONDS = 5
SEND_BACKOFF_DEFER_SECONDS = 10
# Messenger's standard messaging window: free-form replies allowed for 24h
# after the last inbound user message.
CONVERSATION_TTL_SECONDS = 24 * 3600
MID_DEDUP_TTL_SECONDS = 7 * 24 * 3600


class RetryLater(Exception):
    """Signals the ARQ task to defer and re-run the whole job."""

    def __init__(self, message: str, *, defer_seconds: int):
        super().__init__(message)
        self.defer_seconds = defer_seconds


# ----------------------------------------------------------------------
# Redis state
# ----------------------------------------------------------------------

_redis_client: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def _lease_key(page_id: str, psid: str) -> str:
    return f"fb:conv-lease:{page_id}:{psid}"


def _run_key(page_id: str, psid: str) -> str:
    return f"fb:conv-run:{page_id}:{psid}"


def _mid_key(mid: str) -> str:
    return f"fb:mid:{mid}"


async def _acquire_lease(page_id: str, psid: str) -> str:
    r = await _get_redis()
    token = uuid4().hex
    acquired = await r.set(
        _lease_key(page_id, psid), token, nx=True, ex=CONVERSATION_LEASE_TTL_SECONDS
    )
    if not acquired:
        raise RetryLater(
            f"Messenger conversation ({page_id}, {psid}) is busy",
            defer_seconds=LEASE_BUSY_DEFER_SECONDS,
        )
    return token


async def _release_lease(page_id: str, psid: str, token: str) -> None:
    try:
        r = await _get_redis()
        if await r.get(_lease_key(page_id, psid)) == token:
            await r.delete(_lease_key(page_id, psid))
    except Exception as e:
        logger.warning(f"Failed to release Messenger conversation lease: {e}")


# ----------------------------------------------------------------------
# Text-session helpers (mirror the WhatsApp runtime)
# ----------------------------------------------------------------------


def bootstrap_text_session_data() -> dict[str, Any]:
    """User-first text session payload (no agent-seeded greeting turn)."""
    return {
        "version": 1,
        "status": "idle",
        "cursor_turn_id": None,
        "turns": [],
        "discarded_future": [],
        "simulator": {"enabled": False, "config": {}},
    }


def latest_assistant_text(session_data: dict[str, Any]) -> str | None:
    turns = list(session_data.get("turns") or [])
    if not turns:
        return None
    last_turn = turns[-1] or {}
    if last_turn.get("status") != "completed":
        return None
    text = ((last_turn.get("assistant_message") or {}).get("text") or "").strip()
    return text or None


def _inbound_text(msg) -> str:
    """Collapse a parsed Messenger event into the user-turn text."""
    if msg.text:
        return msg.text.strip()
    if msg.quick_reply_payload:
        return msg.quick_reply_payload.strip()
    if msg.postback_title:
        return msg.postback_title.strip()
    if msg.postback_payload:
        return msg.postback_payload.strip()
    # Attachments (images/audio/…) are not yet supported on this channel.
    return ""


# ----------------------------------------------------------------------
# Inbound handling
# ----------------------------------------------------------------------


async def handle_inbound_message(page_id: str, msg) -> None:
    """Process one parsed inbound Messenger event end to end."""
    psid = msg.sender_id
    if not page_id or not psid:
        logger.warning(
            f"Dropping Messenger event with missing routing "
            f"(page_id={page_id!r}, psid={psid!r})"
        )
        return

    address = await db_client.get_messaging_address_by_external_id(page_id)
    if address is None:
        logger.warning(
            f"No active messaging address for page_id={page_id}; "
            f"dropping Messenger message"
        )
        return

    lease_token = await _acquire_lease(page_id, psid)
    try:
        r = await _get_redis()
        if msg.mid and not await r.set(
            _mid_key(msg.mid), "1", nx=True, ex=MID_DEDUP_TTL_SECONDS
        ):
            logger.debug(f"Skipping already-processed Messenger message {msg.mid}")
            return
        await _process(page_id, address, msg)
    finally:
        await _release_lease(page_id, psid, lease_token)


async def _get_or_open_run(page_id: str, address, psid: str) -> int | None:
    """Return the conversation's workflow run id, creating one if needed."""
    r = await _get_redis()
    existing = await r.get(_run_key(page_id, psid))
    if existing:
        try:
            return int(existing)
        except (TypeError, ValueError):
            pass

    if not address.inbound_workflow_id:
        logger.warning(
            f"Messaging address {address.id} (page_id={page_id}) has no inbound "
            f"workflow assigned; dropping Messenger message from {psid}"
        )
        return None

    workflow = await db_client.get_workflow(
        address.inbound_workflow_id, organization_id=address.organization_id
    )
    if workflow is None:
        logger.warning(
            f"Inbound workflow {address.inbound_workflow_id} not found for "
            f"org {address.organization_id}; dropping Messenger message"
        )
        return None

    workflow_run = await db_client.create_workflow_run(
        name=f"WR-FB-{uuid4().hex[:6]}",
        workflow_id=address.inbound_workflow_id,
        mode=WorkflowRunMode.FACEBOOK.value,
        user_id=workflow.user_id,
        call_type=CallType.INBOUND,
        initial_context={
            "provider": "facebook",
            "psid": psid,
            "page_id": page_id,
        },
        organization_id=address.organization_id,
    )

    from api.services.quota_service import authorize_workflow_run_start

    quota_result = await authorize_workflow_run_start(
        workflow_id=address.inbound_workflow_id,
        organization_id=address.organization_id,
        workflow_run_id=workflow_run.id,
    )
    if not quota_result.has_quota:
        error_message = quota_result.error_message or "Quota exceeded"
        logger.warning(
            f"Messenger inbound quota check failed for workflow run "
            f"{workflow_run.id}: {error_message}"
        )
        await _abandon_run(workflow_run.id, error_message)
        return None

    from api.services.workflow.text_chat_runner import default_text_chat_checkpoint

    await db_client.ensure_workflow_run_text_session(
        workflow_run.id,
        session_data=bootstrap_text_session_data(),
        checkpoint=default_text_chat_checkpoint(),
    )
    await r.set(_run_key(page_id, psid), str(workflow_run.id), ex=CONVERSATION_TTL_SECONDS)
    return workflow_run.id


async def _abandon_run(run_id: int, error_message: str) -> None:
    try:
        await db_client.update_workflow_run(
            run_id=run_id,
            is_completed=True,
            state=WorkflowRunState.COMPLETED.value,
        )
    except Exception:
        logger.exception(f"Failed to abandon Messenger workflow run {run_id}")


async def _process(page_id: str, address, msg) -> None:
    from api.services.messaging.facebook.client import (
        MessengerApiError,
        client_for_configuration,
    )

    psid = msg.sender_id
    run_id = await _get_or_open_run(page_id, address, psid)
    if run_id is None:
        return

    r = await _get_redis()
    # Any inbound activity refreshes the 24h window.
    await r.expire(_run_key(page_id, psid), CONVERSATION_TTL_SECONDS)

    normalized_text = _inbound_text(msg)
    if not normalized_text:
        logger.info(
            f"Ignoring Messenger event with no usable text "
            f"(page_id={page_id}, psid={psid})"
        )
        return

    client = client_for_configuration(address.configuration, page_id)
    await client.send_action(psid, "mark_seen")
    await client.send_action(psid, "typing_on")

    from pipecat.utils.run_context import set_current_run_id

    from api.services.workflow.text_chat_session_service import (
        TextChatPendingTurnLostError,
        TextChatSessionExecutionError,
        append_text_chat_user_message,
        execute_pending_text_chat_turn,
        normalize_text_chat_session_data,
    )

    set_current_run_id(run_id)

    text_session = await db_client.get_workflow_run_text_session(
        run_id, organization_id=address.organization_id
    )
    if text_session is None:
        logger.error(
            f"No text session for Messenger workflow run {run_id}; dropping message"
        )
        return

    text_session = await append_text_chat_user_message(
        run_id=run_id,
        text_session=text_session,
        user_text=normalized_text,
        expected_revision=text_session.revision,
    )

    try:
        text_session = await execute_pending_text_chat_turn(
            workflow_id=text_session.workflow_run.workflow_id,
            run_id=run_id,
            text_session=text_session,
        )
    except (TextChatSessionExecutionError, TextChatPendingTurnLostError) as e:
        logger.error(f"Messenger assistant turn failed for run {run_id}: {e}")
        return

    session_data = normalize_text_chat_session_data(text_session.session_data)
    assistant_text = latest_assistant_text(session_data)
    if not assistant_text:
        return

    try:
        from api.services.subscription.enforcement import (
            check_whatsapp_message_allowed,
        )

        subscription_check = await check_whatsapp_message_allowed(
            address.organization_id
        )
        if not subscription_check.allowed:
            logger.warning(
                f"Withholding Messenger reply for run {run_id} "
                f"(org {address.organization_id}): {subscription_check.error_code}"
            )
            return
    except Exception:
        # A billing-check failure must never break a live conversation.
        logger.exception("Messenger subscription check errored; sending reply anyway")

    try:
        outbound_mid = await client.send_text(psid, assistant_text)
        logger.debug(f"Sent Messenger reply {outbound_mid} for run {run_id}")
    except MessengerApiError as e:
        logger.error(
            f"Failed to send Messenger reply for run {run_id} "
            f"(code={e.code}, retry_kind={e.retry_kind}): {e.message}"
        )
        if e.retry_kind == "backoff":
            raise RetryLater(
                f"Messenger send hit a transient error for {psid}",
                defer_seconds=SEND_BACKOFF_DEFER_SECONDS,
            ) from e
        return

    workflow_run = text_session.workflow_run
    if workflow_run is not None and workflow_run.is_completed:
        await r.delete(_run_key(page_id, psid))
        from api.tasks.arq import enqueue_job
        from api.tasks.function_names import FunctionNames

        await enqueue_job(FunctionNames.PROCESS_WORKFLOW_COMPLETION, run_id)
