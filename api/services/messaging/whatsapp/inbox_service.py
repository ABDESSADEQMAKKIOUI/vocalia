"""WhatsApp inbox: conversation listing, thread reading, human replies.

Backs the org-facing inbox routes (``api/routes/messaging_inbox.py``): a
human operator can browse WhatsApp conversations, read the exchanged
messages, pause the agent for a conversation (human takeover) and send
free-form replies while Meta's 24h customer-service window is open.

Threads are rebuilt from the run's text-chat session (the same
``session_data.turns`` the conversation runtime maintains) rather than a
dedicated message table. Human replies are recorded as completed turns
with ``origin: "human"`` AND appended to the checkpoint's serialized LLM
context so the agent keeps full context when it is un-paused.

Heavy dependencies (the Cloud API client, the pipecat-backed checkpoint
default) are imported lazily so this module stays cheap to import for
routes and unit tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from loguru import logger

from api.db import db_client
from api.db.workflow_run_text_session_client import (
    WorkflowRunTextSessionRevisionConflictError,
)
from api.services.messaging.whatsapp.conversation_service import (
    SERVICE_WINDOW_HOURS,
    bootstrap_whatsapp_text_session_data,
)

if TYPE_CHECKING:
    from api.db.models import (
        WhatsAppConversationModel,
        WorkflowRunModel,
        WorkflowRunTextSessionModel,
    )

PREVIEW_MAX_CHARS = 80

# User-facing error for a send outside Meta's 24h customer-service window.
WINDOW_CLOSED_MESSAGE = (
    "La fenêtre de service de 24h est fermée — le contact doit écrire en premier."
)

# Thread label for the campaign template that opened the conversation when
# the template name could not be resolved (template deleted, org mismatch).
TEMPLATE_SENT_FALLBACK_LABEL = "[Message modèle envoyé]"

# How many times a human-reply session write is retried on a revision
# conflict (a webhook worker may commit between our read and write).
_RECORD_REPLY_ATTEMPTS = 3


# ----------------------------------------------------------------------
# Pure helpers (unit-tested without mocks — see tests/messaging)
# ----------------------------------------------------------------------


def truncate_preview(text: str | None, *, max_chars: int = PREVIEW_MAX_CHARS) -> str | None:
    """Collapse a message text to a list-view preview of at most *max_chars*."""
    if text is None:
        return None
    normalized = " ".join(text.split())
    if not normalized:
        return None
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def human_reply_window_open(
    *,
    service_window_expires_at: datetime | None,
    last_inbound_at: datetime | None,
    now: datetime,
) -> bool:
    """True while a free-form (non-template) human reply may still be sent.

    The stored expiry is authoritative when present; ``last_inbound_at + 24h``
    is the fallback for rows that predate the expiry column being touched.
    """
    if service_window_expires_at is not None and now < service_window_expires_at:
        return True
    if (
        last_inbound_at is not None
        and last_inbound_at + timedelta(hours=SERVICE_WINDOW_HOURS) > now
    ):
        return True
    return False


def _iso(value: Any) -> str | None:
    """Normalize a timestamp (datetime | ISO str | None) to an ISO string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _template_send_recorded(run: WorkflowRunModel | None) -> bool:
    """Whether the run carries evidence of the initial campaign template send.

    ``campaign_message_dispatcher`` stores the send wamid as
    ``gathered_context.call_id`` (with ``provider: "whatsapp"``) and appends
    delivery statuses to ``logs.whatsapp_status_callbacks`` — either is proof.
    """
    if run is None:
        return False
    gathered = getattr(run, "gathered_context", None) or {}
    if gathered.get("call_id") and gathered.get("provider") == "whatsapp":
        return True
    run_logs = getattr(run, "logs", None) or {}
    return bool(run_logs.get("whatsapp_status_callbacks"))


def build_thread(
    text_session: WorkflowRunTextSessionModel | None,
    conversation: WhatsAppConversationModel | None = None,
    run: WorkflowRunModel | None = None,
    *,
    template_name: str | None = None,
    template_text: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten a text session's turns into inbox messages, chronological.

    Each turn contributes its user message (direction "in") then its
    assistant message (direction "out", origin "agent" — or "human" for
    turns recorded by :func:`send_human_reply`). For campaign-opened
    conversations, the initial template send (recorded by the dispatcher on
    the run, not in the text session) is prepended as an origin "template"
    entry.
    """
    messages: list[dict[str, Any]] = []

    if (
        conversation is not None
        and conversation.campaign_id
        and _template_send_recorded(run)
    ):
        # Prefer the rendered message as delivered; the technical template
        # name is only a fallback for sends made before template_text was
        # stored (and the generic label a last resort).
        if template_text and template_text.strip():
            label = template_text
        elif template_name:
            label = f"[Message modèle: {template_name}]"
        else:
            label = TEMPLATE_SENT_FALLBACK_LABEL
        # The conversation row is created immediately after the template
        # send, so created_at is the closest stored timestamp for it
        # (last_outbound_at moves forward with every later reply).
        timestamp = conversation.created_at or conversation.last_outbound_at
        messages.append(
            {
                "direction": "out",
                "origin": "template",
                "text": label,
                "timestamp": _iso(timestamp),
            }
        )

    session_data = (getattr(text_session, "session_data", None) or {}) if text_session else {}
    for turn in session_data.get("turns") or []:
        turn = turn or {}
        user_message = turn.get("user_message") or {}
        user_text = (user_message.get("text") or "").strip()
        if user_text:
            messages.append(
                {
                    "direction": "in",
                    "origin": "user",
                    "text": user_text,
                    "timestamp": _iso(user_message.get("created_at")),
                }
            )
        assistant_message = turn.get("assistant_message") or {}
        assistant_text = (assistant_message.get("text") or "").strip()
        if assistant_text:
            origin = "human" if turn.get("origin", "agent") == "human" else "agent"
            messages.append(
                {
                    "direction": "out",
                    "origin": origin,
                    "text": assistant_text,
                    "timestamp": _iso(assistant_message.get("created_at")),
                }
            )
    return messages


def last_message_preview(session_data: dict[str, Any] | None) -> str | None:
    """Text of the most recent message in the session, truncated for lists."""
    for turn in reversed(list((session_data or {}).get("turns") or [])):
        turn = turn or {}
        assistant_text = ((turn.get("assistant_message") or {}).get("text") or "").strip()
        if assistant_text:
            return truncate_preview(assistant_text)
        user_text = ((turn.get("user_message") or {}).get("text") or "").strip()
        if user_text:
            return truncate_preview(user_text)
    return None


def build_human_reply_turn(text: str, *, now_iso: str | None = None) -> dict[str, Any]:
    """A completed, human-origin turn mirroring the runtime turn shape."""
    created_at = now_iso or datetime.now(UTC).isoformat()
    return {
        "id": f"turn_{uuid4().hex[:12]}",
        "status": "completed",
        "created_at": created_at,
        "user_message": None,
        "assistant_message": {"text": text, "created_at": created_at},
        "origin": "human",
        "events": [],
        "usage": {},
    }


# ----------------------------------------------------------------------
# Conversation summaries (list + detail)
# ----------------------------------------------------------------------


def _conversation_summary(
    conversation: WhatsAppConversationModel,
    *,
    text_session: WorkflowRunTextSessionModel | None,
    run: WorkflowRunModel | None,
) -> dict[str, Any]:
    workflow = getattr(run, "workflow", None) if run is not None else None
    session_data = getattr(text_session, "session_data", None) if text_session else None

    preview = last_message_preview(session_data)
    if preview is None and conversation.campaign_id and _template_send_recorded(run):
        # Show the delivered template text when the dispatcher stored it;
        # generic label only for sends predating template_text.
        stored_text = ((getattr(run, "gathered_context", None) or {}).get(
            "template_text"
        ) if run is not None else None)
        preview = truncate_preview(stored_text) or TEMPLATE_SENT_FALLBACK_LABEL

    address = conversation.messaging_address
    return {
        "id": conversation.id,
        "wa_id": conversation.wa_id,
        "profile_name": conversation.profile_name,
        "phone_number_id": conversation.phone_number_id,
        "display_number": address.address if address is not None else None,
        "state": conversation.state,
        "agent_paused": conversation.agent_paused,
        "service_window_expires_at": conversation.service_window_expires_at,
        "last_inbound_at": conversation.last_inbound_at,
        "last_outbound_at": conversation.last_outbound_at,
        "workflow_run_id": conversation.workflow_run_id,
        "campaign_id": conversation.campaign_id,
        "agent_name": workflow.name if workflow is not None else None,
        "updated_at": conversation.updated_at,
        "last_message_preview": preview,
    }


async def _load_session_and_run(
    conversation: WhatsAppConversationModel, *, organization_id: int
) -> tuple[WorkflowRunTextSessionModel | None, WorkflowRunModel | None]:
    """Fetch the conversation's text session (org-scoped) and workflow run.

    The org-scoped session getter eager-loads workflow_run + workflow, so one
    query usually covers both. Campaign conversations have no text session
    until the contact replies — fall back to fetching the run directly
    (``get_workflow_run_by_id`` eager-loads the workflow; the run id comes
    from the org-scoped conversation row, so this is not an org-scope leak).
    """
    text_session = await db_client.get_workflow_run_text_session(
        conversation.workflow_run_id, organization_id=organization_id
    )
    if text_session is not None:
        return text_session, text_session.workflow_run
    run = await db_client.get_workflow_run_by_id(conversation.workflow_run_id)
    return None, run


async def list_conversations(
    organization_id: int,
    *,
    state: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Inbox list, most recently active first, per the inbox HTTP contract.

    NOTE: agent_name / last_message_preview need the run's text session; no
    batch-by-run-ids helper exists, so sessions are fetched individually —
    bounded by the page size (<= ~50 rows), not the table size.
    """
    conversations = await db_client.list_whatsapp_conversations(
        organization_id, state=state, limit=limit, offset=offset
    )
    summaries: list[dict[str, Any]] = []
    for conversation in conversations:
        text_session, run = await _load_session_and_run(
            conversation, organization_id=organization_id
        )
        summaries.append(
            _conversation_summary(conversation, text_session=text_session, run=run)
        )
    return summaries


async def _resolve_campaign_template_display(
    conversation: WhatsAppConversationModel,
    run: WorkflowRunModel | None,
    *,
    organization_id: int,
) -> tuple[str | None, str | None]:
    """(rendered_text, template_name) for the template that opened a
    campaign conversation, org-scoped.

    The rendered text is what the recipient actually received: preferably
    the dispatcher-stored ``gathered_context.template_text``, else it is
    re-rendered from the template mirror + the run's initial context
    (covers sends made before template_text was stored).
    """
    if not conversation.campaign_id:
        return None, None

    stored_text = (
        (getattr(run, "gathered_context", None) or {}).get("template_text")
        if run is not None
        else None
    )

    campaign = await db_client.get_campaign_by_id(conversation.campaign_id)
    if (
        campaign is None
        or campaign.organization_id != organization_id
        or not campaign.whatsapp_template_id
    ):
        return stored_text, None
    template = await db_client.get_whatsapp_template(
        campaign.whatsapp_template_id, organization_id=organization_id
    )
    if template is None:
        return stored_text, None
    if stored_text:
        return stored_text, template.name

    from api.services.messaging.whatsapp.template_service import (
        render_template_display_text,
    )

    values = {
        str(k).lower(): str(v)
        for k, v in ((getattr(run, "initial_context", None) or {}) if run else {}).items()
    }
    rendered = render_template_display_text(
        template.components, template.parameter_format, values
    )
    return (rendered or None), template.name


async def get_conversation_detail(
    organization_id: int, conversation_id: int
) -> dict[str, Any] | None:
    """{conversation, messages} for one conversation; None when it does not
    exist or belongs to another organization (route answers 404)."""
    conversation = await db_client.get_whatsapp_conversation(
        conversation_id, organization_id=organization_id
    )
    if conversation is None:
        return None

    text_session, run = await _load_session_and_run(
        conversation, organization_id=organization_id
    )
    template_text, template_name = await _resolve_campaign_template_display(
        conversation, run, organization_id=organization_id
    )
    return {
        "conversation": _conversation_summary(
            conversation, text_session=text_session, run=run
        ),
        "messages": build_thread(
            text_session,
            conversation,
            run,
            template_name=template_name,
            template_text=template_text,
        ),
    }


# ----------------------------------------------------------------------
# Human reply
# ----------------------------------------------------------------------


async def send_human_reply(
    organization_id: int, conversation_id: int, text: str
) -> dict[str, Any] | None:
    """Send a free-form operator reply and record it in the run's session.

    Returns the fresh conversation detail payload, None for an unknown /
    foreign-org conversation (route answers 404). Raises ValueError with a
    user-facing message when the 24h service window is closed or the Cloud
    API send fails (route answers 400).
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Le message est vide.")

    conversation = await db_client.get_whatsapp_conversation(
        conversation_id, organization_id=organization_id
    )
    if conversation is None:
        return None

    if not human_reply_window_open(
        service_window_expires_at=conversation.service_window_expires_at,
        last_inbound_at=conversation.last_inbound_at,
        now=datetime.now(UTC),
    ):
        raise ValueError(WINDOW_CLOSED_MESSAGE)

    # Address + configuration via the inbound-routing getter (configuration
    # eager-loaded). Guard the org explicitly — the getter is global.
    address = await db_client.get_messaging_address_by_external_id(
        conversation.phone_number_id
    )
    if address is None or address.organization_id != organization_id:
        raise ValueError(
            "Le numéro d'entreprise de cette conversation n'est plus actif."
        )

    from api.services.subscription.enforcement import check_whatsapp_message_allowed

    subscription_check = await check_whatsapp_message_allowed(organization_id)
    if not subscription_check.allowed:
        logger.warning(
            f"Human WhatsApp reply blocked by subscription for conversation "
            f"{conversation.id} (org {organization_id}): "
            f"{subscription_check.error_code}"
        )
        raise ValueError(subscription_check.error_message)

    from api.services.messaging.whatsapp.client import (
        WhatsAppApiError,
        client_for_configuration,
    )

    client = client_for_configuration(
        address.configuration, conversation.phone_number_id
    )
    try:
        outbound_wamid = await client.send_text(to=conversation.wa_id, body=text)
    except WhatsAppApiError as e:
        logger.error(
            f"Human WhatsApp reply failed for conversation {conversation.id} "
            f"(code={e.code}, retry_kind={e.retry_kind}): {e.message}"
        )
        raise ValueError(f"Échec de l'envoi WhatsApp : {e.message}") from e

    logger.info(
        f"Human reply {outbound_wamid} sent for WhatsApp conversation "
        f"{conversation.id} (run {conversation.workflow_run_id})"
    )

    await _record_human_reply(
        conversation.workflow_run_id,
        organization_id=organization_id,
        text=text,
    )
    await db_client.touch_whatsapp_conversation(
        conversation.id, last_outbound_at=datetime.now(UTC)
    )

    return await get_conversation_detail(organization_id, conversation_id)


async def _record_human_reply(
    run_id: int, *, organization_id: int, text: str
) -> None:
    """Append the sent human reply to the run's text session + checkpoint.

    The turn lands in ``session_data.turns`` (origin "human") and the text is
    appended to the checkpoint's serialized LLM context as a plain
    ``{"role": "assistant", "content": ...}`` message — the format the
    text-chat runner round-trips for standard messages — so a later agent
    turn sees the human reply as its own prior output.

    Best-effort AFTER a successful send: retried on revision conflicts (a
    webhook worker may write concurrently), logged if it still fails —
    failing the request for a message that already reached the contact
    would be worse than a gap in the transcript.
    """
    for _ in range(_RECORD_REPLY_ATTEMPTS):
        text_session = await _load_or_bootstrap_session(
            run_id, organization_id=organization_id
        )
        if text_session is None:
            logger.error(
                f"No text session for run {run_id}; human reply not recorded"
            )
            return

        session_data = dict(text_session.session_data or {})
        session_data["turns"] = [
            *(session_data.get("turns") or []),
            build_human_reply_turn(text),
        ]

        checkpoint = dict(text_session.checkpoint or {})
        checkpoint["messages"] = [
            *(checkpoint.get("messages") or []),
            {"role": "assistant", "content": text},
        ]

        try:
            await db_client.update_workflow_run_text_session(
                run_id,
                session_data=session_data,
                checkpoint=checkpoint,
                expected_revision=text_session.revision,
            )
            return
        except WorkflowRunTextSessionRevisionConflictError:
            continue

    logger.error(
        f"Human reply for run {run_id} was sent but could not be recorded "
        f"after {_RECORD_REPLY_ATTEMPTS} revision conflicts"
    )


async def _load_or_bootstrap_session(
    run_id: int, *, organization_id: int
) -> WorkflowRunTextSessionModel | None:
    """Org-scoped session fetch, creating the user-first session if missing
    (campaign conversations only get one on the first inbound message)."""
    text_session = await db_client.get_workflow_run_text_session(
        run_id, organization_id=organization_id
    )
    if text_session is not None:
        return text_session

    # Lazy: default_text_chat_checkpoint lives in the pipecat-heavy runner.
    from api.services.workflow.text_chat_runner import default_text_chat_checkpoint

    await db_client.ensure_workflow_run_text_session(
        run_id,
        session_data=bootstrap_whatsapp_text_session_data(),
        checkpoint=default_text_chat_checkpoint(),
    )
    return await db_client.get_workflow_run_text_session(
        run_id, organization_id=organization_id
    )


# ----------------------------------------------------------------------
# Agent pause (human takeover)
# ----------------------------------------------------------------------


async def set_agent_paused(
    organization_id: int, conversation_id: int, paused: bool
) -> bool:
    """Toggle human takeover; False when the conversation is unknown/foreign."""
    updated = await db_client.set_whatsapp_conversation_agent_paused(
        conversation_id, organization_id=organization_id, paused=paused
    )
    if updated:
        logger.info(
            f"WhatsApp conversation {conversation_id} agent_paused set to "
            f"{paused} (org {organization_id})"
        )
    return updated
