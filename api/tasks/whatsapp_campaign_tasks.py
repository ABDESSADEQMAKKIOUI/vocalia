"""ARQ task for WhatsApp campaign batch processing.

Mirrors ``process_campaign_batch``: on success it publishes a
``batch_completed`` event, which the campaign orchestrator uses to schedule
the next batch after re-checking campaign state, schedule windows and the
circuit breaker (the same chaining used by voice batches — the orchestrator
picks the channel-appropriate task). On failure it publishes
``batch_failed`` and marks the campaign failed.
"""

from typing import Dict

from loguru import logger

from api.db import db_client
from api.services.campaign.campaign_event_publisher import (
    get_campaign_event_publisher,
)
from api.services.campaign.campaign_message_dispatcher import (
    campaign_message_dispatcher,
)


async def process_whatsapp_campaign_batch(
    ctx: Dict, campaign_id: int, batch_size: int = 10
) -> None:
    """Process a batch of queued WhatsApp sends for a campaign.

    Stop conditions live in ``process_whatsapp_batch`` (campaign not in
    'running' state or no claimable queued runs → processed_count 0) and in
    the orchestrator, which only schedules another batch while the campaign
    is running and has pending work.
    """
    logger.info(
        f"Processing WhatsApp batch for campaign {campaign_id}, "
        f"batch_size={batch_size}"
    )

    try:
        processed_count = await campaign_message_dispatcher.process_whatsapp_batch(
            campaign_id=campaign_id, batch_size=batch_size
        )

        # Publish batch completed event - orchestrator will handle next batch
        # scheduling
        publisher = await get_campaign_event_publisher()
        await publisher.publish_batch_completed(
            campaign_id=campaign_id,
            processed_count=processed_count,
            failed_count=0,
            batch_size=batch_size,
        )

        logger.info(
            f"Campaign {campaign_id} WhatsApp batch completed: "
            f"processed={processed_count}"
        )

    except Exception as e:
        logger.error(
            f"Error processing WhatsApp batch for campaign {campaign_id}: {e}"
        )

        # Publish batch failed event
        publisher = await get_campaign_event_publisher()
        await publisher.publish_batch_failed(
            campaign_id=campaign_id,
            error=str(e),
            processed_count=0,
        )

        # Update campaign state to failed
        await db_client.update_campaign(campaign_id=campaign_id, state="failed")
        await db_client.append_campaign_log(
            campaign_id=campaign_id,
            level="error",
            event="batch_failed",
            message=f"WhatsApp batch processing failed: {e}",
            details={"error": str(e), "channel": "whatsapp"},
        )
        raise
