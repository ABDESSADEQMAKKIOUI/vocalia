"""WhatsApp conversation runtime: inbound message -> assistant turn -> reply.

One open WhatsApp conversation window maps to one workflow run
(mode=whatsapp). Assistant turns are produced by reusing the text-chat
session machinery (``api.services.workflow.text_chat_session_service``):
each inbound message appends a user turn to the run's text session,
executes the pending turn, and the assistant text is sent back through
the WhatsApp Cloud API.

Unlike the browser text-chat flow, WhatsApp is user-first: the session is
bootstrapped WITHOUT the agent-first seeded pending turn
(``bootstrap_whatsapp_text_session_data``), so the first executed turn
already carries user text and the runner never generates an unprompted
greeting (its ``generate_if_no_greeting`` only applies to user-less turns).

Concurrency: processing is serialized per (business number, user) pair via
a short Redis lease. A busy lease — or a transient Cloud API send failure —
raises ``RetryLater``, which the ARQ task converts into an ``arq.Retry``
deferral.

Heavy dependencies (the pipecat-based text chat runner, the Graph API
client, quota service, ARQ) are imported lazily inside functions so that
importing this module stays cheap for the webhook path and unit tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import redis.asyncio as aioredis
from loguru import logger
from sqlalchemy.exc import IntegrityError

from api.constants import REDIS_URL
from api.db import db_client
from api.enums import CallType, WorkflowRunMode, WorkflowRunState

if TYPE_CHECKING:
    from api.db.models import (
        MessagingAddressModel,
        WhatsAppConversationModel,
        WorkflowRunTextSessionModel,
    )
    from api.services.messaging.whatsapp.webhook_parser import ParsedInboundMessage

# Mirrors text_chat_session_service.TEXT_CHAT_SESSION_VERSION without
# importing the pipecat-heavy text chat modules at module import time.
WHATSAPP_TEXT_SESSION_VERSION = 1

# Meta's customer service window: free-form messages are allowed for 24h
# after the last inbound customer message.
SERVICE_WINDOW_HOURS = 24

CONVERSATION_LEASE_TTL_SECONDS = 180
LEASE_BUSY_DEFER_SECONDS = 5
SEND_BACKOFF_DEFER_SECONDS = 10

# Opt-out keywords matched against the full normalized message
# (lowercased, stripped). Kept small on purpose: partial matches like
# "please stop calling me" must reach the workflow, not the suppressor.
STOP_KEYWORDS: frozenset[str] = frozenset(
    {
        "stop",
        "unsubscribe",
        "stop promotions",
        "désabonner",
        "desabonner",
        "arret",
        "arrêt",
    }
)


class RetryLater(Exception):
    """The message could not be processed right now — defer and retry.

    Raised for a busy per-conversation lease or a transient (retry_kind
    "backoff") Cloud API send failure. The ARQ task converts it into
    ``arq.Retry(defer=defer_seconds)``.
    """

    def __init__(
        self, message: str, *, defer_seconds: int = LEASE_BUSY_DEFER_SECONDS
    ):
        super().__init__(message)
        self.defer_seconds = defer_seconds


# ----------------------------------------------------------------------
# Pure helpers (unit-tested without mocks)
# ----------------------------------------------------------------------


def conversation_lease_key(phone_number_id: str, wa_id: str) -> str:
    """Redis key serializing processing for one (business number, user) pair."""
    return f"wa:conv-lease:{phone_number_id}:{wa_id}"


def is_stop_message(text: str | None) -> bool:
    """True when the message is exactly an opt-out keyword (case-insensitive)."""
    if not text:
        return False
    return text.strip().lower() in STOP_KEYWORDS


def normalize_inbound_content(
    message_type: str | None,
    *,
    text: str | None = None,
    interactive_reply_title: str | None = None,
    interactive_reply_id: str | None = None,
    is_voice_note: bool = False,
) -> str:
    """Collapse a parsed inbound message into the user-turn text.

    Returns "" when the message carries nothing usable (location, sticker,
    reaction, ...) — the caller skips the assistant turn but still refreshes
    the service window.
    """
    if is_voice_note or message_type == "audio":
        # TODO(Phase-4): download the media and run STT on inbound voice
        # notes instead of this placeholder.
        return "[Note vocale reçue]"
    if interactive_reply_title or interactive_reply_id:
        # Button / list reply: the chosen option is the user's answer.
        return interactive_reply_title or interactive_reply_id or ""
    if message_type == "image":
        return text or "[Image reçue]"
    if message_type == "document":
        return text or "[Document reçu]"
    return text or ""


def bootstrap_whatsapp_text_session_data() -> dict[str, Any]:
    """Initial text-session payload for a user-first WhatsApp conversation.

    Same shape as ``text_chat_session_service.default_text_chat_session_data``
    but intentionally NOT seeded with the agent-first pending turn that
    ``initialize_text_chat_session`` adds for the browser tester — on
    WhatsApp the end user always speaks first. Kept as a local literal so
    importing this module does not pull in the pipecat-heavy runner.
    """
    return {
        "version": WHATSAPP_TEXT_SESSION_VERSION,
        "status": "idle",
        "cursor_turn_id": None,
        "turns": [],
        "discarded_future": [],
        "simulator": {
            "enabled": False,
            "config": {},
        },
    }


def latest_assistant_text(session_data: dict[str, Any]) -> str | None:
    """Assistant text of the session's last turn, if it completed with one.

    Only the final turn is considered: anything earlier was already sent in
    a previous webhook cycle, and re-sending it would duplicate messages.
    """
    turns = list(session_data.get("turns") or [])
    if not turns:
        return None
    last_turn = turns[-1] or {}
    if last_turn.get("status") != "completed":
        return None
    text = ((last_turn.get("assistant_message") or {}).get("text") or "").strip()
    return text or None


def service_window_open(expires_at: datetime | None, *, now: datetime) -> bool:
    """True while free-form (non-template) messages may still be sent."""
    return expires_at is not None and now < expires_at


# ----------------------------------------------------------------------
# Redis lease
# ----------------------------------------------------------------------

_redis_client: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


async def _acquire_conversation_lease(phone_number_id: str, wa_id: str) -> str:
    """SET NX a lease token for the conversation; raise RetryLater if busy."""
    redis_client = await _get_redis()
    key = conversation_lease_key(phone_number_id, wa_id)
    token = uuid4().hex
    acquired = await redis_client.set(
        key, token, nx=True, ex=CONVERSATION_LEASE_TTL_SECONDS
    )
    if not acquired:
        raise RetryLater(
            f"WhatsApp conversation ({phone_number_id}, {wa_id}) is busy",
            defer_seconds=LEASE_BUSY_DEFER_SECONDS,
        )
    return token


async def _release_conversation_lease(
    phone_number_id: str, wa_id: str, token: str
) -> None:
    """Best-effort release: only delete the lease if we still own it."""
    key = conversation_lease_key(phone_number_id, wa_id)
    try:
        redis_client = await _get_redis()
        current = await redis_client.get(key)
        if current == token:
            await redis_client.delete(key)
    except Exception as e:
        # The lease TTL is the safety net; never fail the job on release.
        logger.warning(f"Failed to release WhatsApp conversation lease {key}: {e}")


# ----------------------------------------------------------------------
# Inbound message handling
# ----------------------------------------------------------------------


async def handle_inbound_message(
    change_phone_number_id: str, msg: ParsedInboundMessage
) -> None:
    """Process one parsed inbound WhatsApp message end to end.

    Resolves the business number to a messaging address, serializes on the
    per-conversation Redis lease, dedupes on wamid, then opens/reuses the
    conversation's workflow run and executes an assistant turn.

    Raises RetryLater when the conversation lease is busy or the reply send
    hit a transient error — the ARQ task defers and retries the job.
    """
    phone_number_id = msg.phone_number_id or change_phone_number_id
    if not phone_number_id or not msg.from_wa_id or not msg.wamid:
        logger.warning(
            "Dropping inbound WhatsApp message with missing routing fields "
            f"(wamid={msg.wamid!r}, phone_number_id={phone_number_id!r}, "
            f"from={msg.from_wa_id!r})"
        )
        return

    address = await db_client.get_messaging_address_by_external_id(phone_number_id)
    if address is None:
        logger.warning(
            f"No active messaging address for phone_number_id={phone_number_id}; "
            f"dropping WhatsApp message {msg.wamid}"
        )
        return

    # The lease is taken BEFORE the wamid idempotency claim on purpose: a
    # wamid claimed by an attempt that then finds the lease busy would make
    # the deferred retry a no-op and silently drop the message.
    lease_token = await _acquire_conversation_lease(phone_number_id, msg.from_wa_id)
    try:
        if not await db_client.try_record_wamid(msg.wamid):
            logger.debug(f"Skipping already-processed WhatsApp message {msg.wamid}")
            return
        await _process_inbound_message(phone_number_id, address, msg)
    finally:
        await _release_conversation_lease(
            phone_number_id, msg.from_wa_id, lease_token
        )


async def _process_inbound_message(
    phone_number_id: str,
    address: MessagingAddressModel,
    msg: ParsedInboundMessage,
) -> None:
    now = datetime.now(UTC)
    wa_id = msg.from_wa_id
    window_expires_at = now + timedelta(hours=SERVICE_WINDOW_HOURS)

    conversation = await db_client.get_open_whatsapp_conversation(
        phone_number_id=phone_number_id, wa_id=wa_id
    )
    if conversation is None:
        conversation = await _open_conversation(
            phone_number_id=phone_number_id, address=address, msg=msg, now=now
        )
        if conversation is None:
            return

    run_id = conversation.workflow_run_id

    normalized_text = normalize_inbound_content(
        msg.message_type,
        text=msg.text,
        interactive_reply_title=msg.interactive_reply_title,
        interactive_reply_id=msg.interactive_reply_id,
        is_voice_note=msg.is_voice_note,
    ).strip()

    if not normalized_text:
        # Unsupported content (location, sticker, reaction, ...) still counts
        # as customer activity for the 24h service window.
        await db_client.touch_whatsapp_conversation(
            conversation.id,
            profile_name=msg.profile_name,
            last_inbound_at=now,
            service_window_expires_at=window_expires_at,
        )
        logger.info(
            f"Ignoring WhatsApp message {msg.wamid} with no usable text content "
            f"(type={msg.message_type})"
        )
        return

    if is_stop_message(normalized_text):
        await db_client.upsert_messaging_suppression(
            organization_id=address.organization_id,
            address=wa_id,
            scope="marketing",
            reason="stop",
            expires_at=None,
        )
        logger.info(
            f"Recorded marketing suppression for {wa_id} "
            f"(org {address.organization_id}) after STOP keyword"
        )
        # The message still reaches the workflow so it can acknowledge the
        # opt-out in-conversation.

    # Human takeover: when the agent is paused for this conversation, record
    # the user message (and refresh the service window) but do NOT execute an
    # agent turn — a human replies from the inbox. Re-fetched by id so a pause
    # toggled after the conversation row was loaded above is still honored.
    fresh_conversation = await db_client.get_whatsapp_conversation(conversation.id)
    if fresh_conversation is not None and fresh_conversation.agent_paused:
        await _record_inbound_without_agent_turn(
            run_id,
            organization_id=address.organization_id,
            user_text=normalized_text,
            wamid=msg.wamid,
        )
        await db_client.touch_whatsapp_conversation(
            conversation.id,
            profile_name=msg.profile_name,
            last_inbound_at=now,
            service_window_expires_at=window_expires_at,
        )
        logger.info(
            f"Agent paused for WhatsApp conversation {conversation.id}; "
            f"recorded message {msg.wamid} without an agent turn"
        )
        return

    from api.services.messaging.whatsapp.client import (
        WhatsAppApiError,
        client_for_configuration,
    )

    client = client_for_configuration(address.configuration, phone_number_id)
    try:
        # Best-effort read receipt + typing indicator.
        await client.mark_read(msg.wamid)
    except Exception as e:
        logger.debug(f"mark_read failed for WhatsApp message {msg.wamid}: {e}")

    from pipecat.utils.run_context import set_current_run_id

    from api.services.workflow.text_chat_session_service import (
        TextChatPendingTurnLostError,
        TextChatSessionExecutionError,
        append_text_chat_user_message,
        execute_pending_text_chat_turn,
        normalize_text_chat_session_data,
    )

    set_current_run_id(run_id)

    text_session = await _load_or_bootstrap_text_session(
        run_id, organization_id=address.organization_id
    )
    if text_session is None:
        logger.error(
            f"No text session for WhatsApp workflow run {run_id}; "
            f"dropping message {msg.wamid}"
        )
        return

    text_session = await append_text_chat_user_message(
        run_id=run_id,
        text_session=text_session,
        user_text=normalized_text,
        expected_revision=text_session.revision,
    )

    await db_client.touch_whatsapp_conversation(
        conversation.id,
        profile_name=msg.profile_name,
        last_inbound_at=now,
        service_window_expires_at=window_expires_at,
    )

    try:
        text_session = await execute_pending_text_chat_turn(
            workflow_id=text_session.workflow_run.workflow_id,
            run_id=run_id,
            text_session=text_session,
        )
    except (TextChatSessionExecutionError, TextChatPendingTurnLostError) as e:
        # The turn is already marked failed in the session; the next inbound
        # message starts a fresh turn from the last good checkpoint.
        logger.error(f"WhatsApp assistant turn failed for run {run_id}: {e}")
        return

    session_data = normalize_text_chat_session_data(text_session.session_data)
    assistant_text = latest_assistant_text(session_data)

    reply_allowed = bool(assistant_text) and service_window_open(
        window_expires_at, now=datetime.now(UTC)
    )
    if reply_allowed:
        from api.services.subscription.enforcement import (
            check_whatsapp_message_allowed,
        )

        subscription_check = await check_whatsapp_message_allowed(
            address.organization_id
        )
        if not subscription_check.allowed:
            # A billing block must never break a live conversation: the user
            # and assistant turns are already persisted, only the outbound
            # reply is withheld.
            logger.warning(
                f"Withholding WhatsApp reply for run {run_id} "
                f"(org {address.organization_id}): {subscription_check.error_code} "
                f"- {subscription_check.error_message}"
            )
            reply_allowed = False

    if reply_allowed:
        try:
            outbound_wamid = await client.send_text(to=wa_id, body=assistant_text)
            await db_client.touch_whatsapp_conversation(
                conversation.id, last_outbound_at=datetime.now(UTC)
            )
            logger.debug(f"Sent WhatsApp reply {outbound_wamid} for run {run_id}")
        except WhatsAppApiError as e:
            logger.error(
                f"Failed to send WhatsApp reply for run {run_id} "
                f"(code={e.code}, retry_kind={e.retry_kind}): {e.message}"
            )
            if e.retry_kind == "backoff":
                # NOTE: the deferred retry will skip this wamid (already
                # recorded), so a send that keeps failing after the deferral
                # is dropped rather than looping forever.
                raise RetryLater(
                    f"WhatsApp send hit a transient error for {wa_id}",
                    defer_seconds=SEND_BACKOFF_DEFER_SECONDS,
                ) from e

    workflow_run = text_session.workflow_run
    if workflow_run is not None and workflow_run.is_completed:
        if await db_client.close_whatsapp_conversation(conversation.id):
            logger.info(
                f"Closed WhatsApp conversation {conversation.id} "
                f"(workflow run {run_id} completed)"
            )
        # Lazy: api.tasks.arq imports task modules at import time.
        from api.tasks.arq import enqueue_job
        from api.tasks.function_names import FunctionNames

        await enqueue_job(FunctionNames.PROCESS_WORKFLOW_COMPLETION, run_id)


async def _open_conversation(
    *,
    phone_number_id: str,
    address: MessagingAddressModel,
    msg: ParsedInboundMessage,
    now: datetime,
) -> WhatsAppConversationModel | None:
    """Create the workflow run + text session + conversation row for a new window.

    Returns None (after logging) when the address has no inbound workflow or
    the org has no quota — the message is dropped.
    """
    wa_id = msg.from_wa_id
    if not address.inbound_workflow_id:
        logger.warning(
            f"Messaging address {address.id} (phone_number_id={phone_number_id}) "
            f"has no inbound workflow assigned; dropping message from {wa_id}"
        )
        return None

    workflow = await db_client.get_workflow(
        address.inbound_workflow_id, organization_id=address.organization_id
    )
    if workflow is None:
        logger.warning(
            f"Inbound workflow {address.inbound_workflow_id} not found for "
            f"org {address.organization_id}; dropping message from {wa_id}"
        )
        return None

    workflow_run = await db_client.create_workflow_run(
        name=f"WR-WA-{uuid4().hex[:6]}",
        workflow_id=address.inbound_workflow_id,
        mode=WorkflowRunMode.WHATSAPP.value,
        user_id=workflow.user_id,
        call_type=CallType.INBOUND,
        initial_context={
            "provider": "whatsapp",
            "wa_id": wa_id,
            "phone_number": f"+{wa_id}",
            "profile_name": msg.profile_name,
            "phone_number_id": phone_number_id,
        },
        organization_id=address.organization_id,
    )

    # Actor-less quota check, mirroring the campaign call dispatcher.
    from api.services.quota_service import authorize_workflow_run_start

    quota_result = await authorize_workflow_run_start(
        workflow_id=address.inbound_workflow_id,
        organization_id=address.organization_id,
        workflow_run_id=workflow_run.id,
    )
    if not quota_result.has_quota:
        error_message = quota_result.error_message or "Quota exceeded"
        logger.warning(
            f"WhatsApp inbound quota check failed for workflow run "
            f"{workflow_run.id}: {error_message}"
        )
        await _abandon_workflow_run(workflow_run.id, error_message)
        return None

    from api.services.workflow.text_chat_runner import default_text_chat_checkpoint

    await db_client.ensure_workflow_run_text_session(
        workflow_run.id,
        session_data=bootstrap_whatsapp_text_session_data(),
        checkpoint=default_text_chat_checkpoint(),
    )

    try:
        return await db_client.create_whatsapp_conversation(
            organization_id=address.organization_id,
            messaging_address_id=address.id,
            phone_number_id=phone_number_id,
            wa_id=wa_id,
            workflow_run_id=workflow_run.id,
            profile_name=msg.profile_name,
            service_window_expires_at=now + timedelta(hours=SERVICE_WINDOW_HOURS),
            last_inbound_at=now,
        )
    except IntegrityError:
        # Webhook redelivery race: another worker opened the conversation
        # between our lookup and insert. Reuse theirs, abandon our run.
        logger.warning(
            f"Open WhatsApp conversation already exists for "
            f"({phone_number_id}, {wa_id}); reusing it"
        )
        await _abandon_workflow_run(
            workflow_run.id,
            "Superseded by a concurrently opened WhatsApp conversation",
        )
        return await db_client.get_open_whatsapp_conversation(
            phone_number_id=phone_number_id, wa_id=wa_id
        )


async def _abandon_workflow_run(run_id: int, error_message: str) -> None:
    """Complete a workflow run that never got a conversation (quota/race)."""
    try:
        await db_client.update_workflow_run(
            run_id=run_id,
            is_completed=True,
            state=WorkflowRunState.COMPLETED.value,
            gathered_context={"error": error_message},
        )
    except Exception as e:
        logger.error(f"Failed to mark abandoned WhatsApp workflow run {run_id}: {e}")


async def _record_inbound_without_agent_turn(
    run_id: int, *, organization_id: int, user_text: str, wamid: str
) -> None:
    """Append the user message as a completed, never-executed turn.

    Used while the agent is paused (human takeover): the turn mirrors the
    pending-turn shape but is stored completed with no assistant message,
    and the user text is appended to the checkpoint's serialized LLM
    context (plain ``{"role": "user", "content": ...}`` — the same shape
    the runner adds for executed turns) so a later un-paused agent turn
    sees the full exchange.
    """
    text_session = await _load_or_bootstrap_text_session(
        run_id, organization_id=organization_id
    )
    if text_session is None:
        logger.error(
            f"No text session for WhatsApp workflow run {run_id}; "
            f"paused-agent message {wamid} not recorded"
        )
        return

    now_iso = datetime.now(UTC).isoformat()
    session_data = dict(text_session.session_data or {})
    session_data["turns"] = [
        *(session_data.get("turns") or []),
        {
            "id": f"turn_{uuid4().hex[:12]}",
            "status": "completed",
            "created_at": now_iso,
            "user_message": {"text": user_text, "created_at": now_iso},
            "assistant_message": None,
            "events": [],
            "usage": {},
        },
    ]

    checkpoint = dict(text_session.checkpoint or {})
    checkpoint["messages"] = [
        *(checkpoint.get("messages") or []),
        {"role": "user", "content": user_text},
    ]

    try:
        await db_client.update_workflow_run_text_session(
            run_id,
            session_data=session_data,
            checkpoint=checkpoint,
            expected_revision=text_session.revision,
        )
    except Exception as e:
        # The lease serializes inbound processing, so conflicts are rare;
        # losing one paused-mode transcript entry must not fail the webhook.
        logger.error(
            f"Failed to record paused-agent WhatsApp message {wamid} "
            f"for run {run_id}: {e}"
        )


async def _load_or_bootstrap_text_session(
    run_id: int, *, organization_id: int
) -> WorkflowRunTextSessionModel | None:
    """Load the run's text session, creating a user-first one if missing.

    Always returns a session fetched through the org-scoped getter so that
    ``workflow_run`` is eager-loaded (``execute_pending_text_chat_turn``
    reads ``workflow_run.usage_info``).
    """
    text_session = await db_client.get_workflow_run_text_session(
        run_id, organization_id=organization_id
    )
    if text_session is not None:
        return text_session

    from api.services.workflow.text_chat_runner import default_text_chat_checkpoint

    await db_client.ensure_workflow_run_text_session(
        run_id,
        session_data=bootstrap_whatsapp_text_session_data(),
        checkpoint=default_text_chat_checkpoint(),
    )
    return await db_client.get_workflow_run_text_session(
        run_id, organization_id=organization_id
    )
