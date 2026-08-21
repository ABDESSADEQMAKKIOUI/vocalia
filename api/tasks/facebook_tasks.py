"""ARQ tasks for the Facebook Messenger channel.

``process_facebook_inbound`` fans one webhook event item (enqueued by the
public webhook route) out to the conversation runtime. A ``RetryLater`` from
the runtime (busy conversation lease or transient Send API failure) is
converted into an arq ``Retry`` so the whole job re-runs after the deferral;
events that already went through are then skipped by mid idempotency.
"""

from typing import Dict

from arq import Retry
from loguru import logger

from api.services.messaging.facebook.conversation_service import (
    RetryLater,
    handle_inbound_message,
)
from api.services.messaging.facebook.webhook_parser import parse_messaging_event


async def process_facebook_inbound(ctx: Dict, change: dict) -> None:
    """Process one webhook event item.

    ``change`` is ``{"kind", "page_id", "event"}`` as produced by
    ``webhook_parser.parse_messenger_payload``.
    """
    page_id = change.get("page_id")
    event = change.get("event") or {}
    msg = parse_messaging_event(page_id, event)

    try:
        await handle_inbound_message(page_id, msg)
    except RetryLater as e:
        logger.info(f"Deferring Messenger inbound job by {e.defer_seconds}s: {e}")
        raise Retry(defer=e.defer_seconds) from e
    except Exception:
        logger.exception(
            f"Failed to process inbound Messenger event (page_id={page_id})"
        )
