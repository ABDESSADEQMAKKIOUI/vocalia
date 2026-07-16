"""ARQ tasks for the WhatsApp messaging channel.

``process_whatsapp_inbound`` fans one webhook change item (enqueued by the
public webhook route) out to the conversation runtime, the campaign status
handler, or the template mirror. ``sweep_whatsapp_conversations`` is the
cron safety net that closes idle conversations and purges expired
bookkeeping rows.
"""

from datetime import UTC, datetime, timedelta
from typing import Dict

from arq import Retry
from loguru import logger

from api.db import db_client
from api.enums import WorkflowRunState
from api.services.messaging.whatsapp.conversation_service import (
    RetryLater,
    handle_inbound_message,
)
from api.tasks.function_names import FunctionNames

WAMID_RETENTION_DAYS = 7
CONVERSATION_INACTIVITY_HOURS = 24


async def process_whatsapp_inbound(ctx: Dict, change: dict) -> None:
    """Process one webhook change item.

    ``change`` is ``{"kind", "phone_number_id", "value"}`` as produced by
    ``webhook_parser.parse_webhook_payload``. A ``RetryLater`` from the
    conversation runtime (busy conversation lease, transient send failure)
    is converted into an arq ``Retry`` so the whole job re-runs after the
    deferral; messages that already went through are then skipped by wamid
    idempotency.
    """
    kind = change.get("kind")
    value = change.get("value") or {}
    change_phone_number_id = change.get("phone_number_id")

    if kind == "messages":
        from api.services.messaging.whatsapp.webhook_parser import (
            parse_messages_value,
        )

        messages, statuses = parse_messages_value(value)

        for message in messages:
            try:
                await handle_inbound_message(change_phone_number_id, message)
            except RetryLater as e:
                logger.info(f"Deferring WhatsApp inbound job by {e.defer_seconds}s: {e}")
                raise Retry(defer=e.defer_seconds) from e
            except Exception:
                # One malformed/broken message must not poison the batch.
                logger.exception(
                    f"Failed to process inbound WhatsApp message {message.wamid}"
                )

        if statuses:
            try:
                # Lazy: the campaign dispatcher is optional at import time.
                from api.services.campaign.campaign_message_dispatcher import (
                    handle_whatsapp_status_event,
                )
            except ImportError as e:
                logger.warning(
                    f"Campaign message dispatcher unavailable; dropping "
                    f"{len(statuses)} WhatsApp status event(s): {e}"
                )
            else:
                for status in statuses:
                    try:
                        await handle_whatsapp_status_event(status)
                    except Exception:
                        logger.exception(
                            f"Failed to process WhatsApp status event "
                            f"{status.get('id')}"
                        )
        return

    if kind and kind.startswith("template"):
        try:
            # Lazy: the template mirror service is optional at import time.
            from api.services.messaging.whatsapp.template_service import (
                handle_template_webhook,
            )
        except ImportError as e:
            logger.warning(
                f"Template service unavailable; dropping {kind} webhook: {e}"
            )
            return
        await handle_template_webhook(kind, value)
        return

    logger.warning(f"Ignoring WhatsApp webhook change with unknown kind: {kind!r}")


async def sweep_whatsapp_conversations(ctx: Dict) -> None:
    """Close idle WhatsApp conversations and purge expired bookkeeping rows.

    - Conversations with no inbound activity for CONVERSATION_INACTIVITY_HOURS
      are closed; their workflow run is completed and post-run processing
      (integrations, billing) is enqueued.
    - Processed-wamid rows older than WAMID_RETENTION_DAYS are purged.
    - Expired messaging suppressions are purged.
    """
    from api.tasks.arq import enqueue_job  # lazy import avoids circular import

    now = datetime.now(UTC)
    inactivity_cutoff = now - timedelta(hours=CONVERSATION_INACTIVITY_HOURS)

    stale_conversations = await db_client.list_stale_whatsapp_conversations(
        inactive_since=inactivity_cutoff
    )
    closed_count = 0
    for conversation in stale_conversations:
        try:
            if not await db_client.close_whatsapp_conversation(conversation.id):
                continue
            closed_count += 1
            # The run ends with the conversation window: mark it completed
            # before the completion pipeline reports usage.
            await db_client.update_workflow_run(
                run_id=conversation.workflow_run_id,
                is_completed=True,
                state=WorkflowRunState.COMPLETED.value,
            )
            await enqueue_job(
                FunctionNames.PROCESS_WORKFLOW_COMPLETION,
                conversation.workflow_run_id,
            )
        except Exception:
            logger.exception(
                f"Failed to close stale WhatsApp conversation {conversation.id}"
            )

    purged_wamids = await db_client.purge_processed_wamids(
        older_than=now - timedelta(days=WAMID_RETENTION_DAYS)
    )
    purged_suppressions = await db_client.purge_expired_messaging_suppressions()

    logger.info(
        f"WhatsApp sweep: closed {closed_count}/{len(stale_conversations)} stale "
        f"conversation(s), purged {purged_wamids} processed wamid(s) and "
        f"{purged_suppressions} expired suppression(s)"
    )
