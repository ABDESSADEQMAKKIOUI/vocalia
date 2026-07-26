"""WhatsApp campaign message dispatching and suppression enforcement.

Mirrors ``campaign_call_dispatcher`` for the messaging channel: claims
queued runs, enforces the do-not-message list, checks quota, creates
workflow runs (mode=whatsapp), sends the campaign's approved template via
the Cloud API and opens the conversation row that maps future inbound
replies onto the run.

Deliberate differences from the voice dispatcher: there is no from_number
pool (one business number sends the whole campaign) and no
call-concurrency slots — sends are paced only by the per-phone-number
rate limiter.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy.exc import IntegrityError

from api.db import db_client
from api.db.models import QueuedRunModel, WorkflowRunModel
from api.enums import CallType, WorkflowRunMode, WorkflowRunState
from api.services.campaign.campaign_event_publisher import (
    get_campaign_event_publisher,
)
from api.services.campaign.circuit_breaker import circuit_breaker
from api.services.campaign.rate_limiter import rate_limiter
from api.services.quota_service import authorize_workflow_run_start
from api.services.subscription.enforcement import check_whatsapp_message_allowed

# NOTE: api.services.messaging.whatsapp.* is imported lazily inside methods —
# mirroring how campaign_call_dispatcher lazy-imports telephony — so this
# module stays importable (task registration, unit tests) without pulling in
# the messaging client stack.

MARKETING_CATEGORY = "MARKETING"

# Dispatcher actions for a failed template send, keyed by
# WhatsAppApiError.retry_kind.
ACTION_RETRY = "retry"
ACTION_SUPPRESS_24H = "suppress_24h"
ACTION_SUPPRESS_PERMANENT = "suppress_permanent"
ACTION_HALT = "halt"
ACTION_FAIL = "fail"

RETRY_KIND_ACTIONS: dict[str, str] = {
    "backoff": ACTION_RETRY,
    "cooldown_24h": ACTION_SUPPRESS_24H,
    "suppress_permanent": ACTION_SUPPRESS_PERMANENT,
    "campaign_halt": ACTION_HALT,
    "never": ACTION_FAIL,
}

# Meta delivery-failure error codes that must feed the suppression list:
# code -> (scope, reason, ttl). ttl=None means permanent.
# 131049: per-user marketing message cap (healthy-ecosystem limit) — cool
#         down for 24h. 131050: user opted out of marketing messages at the
#         WhatsApp level — permanent marketing suppression.
META_ERROR_SUPPRESSIONS: dict[int, tuple[str, str, timedelta | None]] = {
    131049: ("marketing", "meta_131049", timedelta(hours=24)),
    131050: ("marketing", "meta_131050", None),
}


def normalize_wa_id(phone_number: object) -> str:
    """Normalize an E.164-ish phone number to a Meta wa_id (digits only).

    Strips the leading '+' plus any spaces, dashes or parentheses. Returns
    an empty string when no digits are present.
    """
    return "".join(ch for ch in str(phone_number or "") if ch.isdigit())


def is_marketing_template(category: str | None) -> bool:
    """Whether a template category makes sends subject to marketing
    suppressions. Marketing sends are blocked by both suppression scopes
    ('all' and 'marketing'); utility/authentication only by 'all'."""
    return (category or "").strip().upper() == MARKETING_CATEGORY


def action_for_retry_kind(retry_kind: str | None) -> str:
    """Map a WhatsAppApiError.retry_kind to a dispatcher action. Unknown or
    missing kinds fail the queued run without retrying."""
    return RETRY_KIND_ACTIONS.get(retry_kind or "", ACTION_FAIL)


def suppression_for_meta_error_code(
    code: object,
) -> tuple[str, str, timedelta | None] | None:
    """Return (scope, reason, ttl) when a Meta error code requires a
    suppression-list entry, else None."""
    try:
        return META_ERROR_SUPPRESSIONS.get(int(code))
    except (TypeError, ValueError):
        return None


def lowercase_template_values(context_variables: dict | None) -> dict[str, str]:
    """Build the template-parameter value map from queued-run context.

    CSV headers are lowercased by source validation and named template
    placeholders are enforced lowercase at campaign creation, so values are
    keyed by the lowercased context keys, stringified for the send payload.
    """
    values: dict[str, str] = {}
    for key, value in (context_variables or {}).items():
        values[str(key).lower()] = "" if value is None else str(value)
    return values


class CampaignMessageDispatcher:
    """Rate-limited WhatsApp template dispatching for campaigns."""

    async def process_whatsapp_batch(
        self, campaign_id: int, batch_size: int = 10
    ) -> int:
        """Process a batch of queued runs for a WhatsApp campaign.

        Thread-safe: uses SELECT FOR UPDATE SKIP LOCKED claiming, mirroring
        the voice dispatcher. Returns the number of successfully dispatched
        sends.
        """
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        if campaign.state != "running":
            logger.info(
                f"Campaign {campaign_id} is not in running state: {campaign.state}"
            )
            return 0

        queued_runs = await db_client.claim_queued_runs_for_processing(
            campaign_id=campaign_id,
            scheduled_before=datetime.now(UTC),
            limit=batch_size,
        )

        if not queued_runs:
            logger.info(f"No more queued runs for campaign {campaign_id}")
            return 0

        try:
            config, template, address = await self._resolve_messaging_resources(
                campaign
            )
        except Exception:
            await self._return_unprocessed_claims(
                queued_runs, set(), reason="messaging_resources_unavailable"
            )
            raise

        marketing = is_marketing_template(template.category)
        phone_number_id = address.external_id

        processed_count = 0
        processed_run_ids: set[int] = set()
        for queued_run in queued_runs:
            try:
                # Pace sends per business phone number — Meta throttles by
                # phone_number_id, not by org.
                await self.apply_rate_limit(
                    phone_number_id, campaign.rate_limit_per_second
                )

                workflow_run = await self.dispatch_message(
                    queued_run,
                    campaign,
                    config=config,
                    template=template,
                    address=address,
                    marketing=marketing,
                )

                processed_run_ids.add(queued_run.id)
                if workflow_run is None:
                    # Suppressed recipient or terminal API error — the queued
                    # run was already marked failed inside dispatch_message.
                    continue

                await db_client.update_queued_run(
                    queued_run_id=queued_run.id,
                    state="processed",
                    workflow_run_id=workflow_run.id,
                    processed_at=datetime.now(UTC),
                )

                processed_count += 1

                await db_client.update_campaign(
                    campaign_id=campaign_id,
                    processed_rows=campaign.processed_rows + processed_count,
                )

            except asyncio.CancelledError:
                logger.warning(
                    f"Campaign {campaign_id} WhatsApp batch cancelled; returning "
                    "claimed queued runs that were not dispatched"
                )
                await self._return_unprocessed_claims(
                    queued_runs, processed_run_ids, reason="task_cancelled"
                )
                raise

            except Exception as e:
                logger.warning(f"Error processing queued run {queued_run.id}: {e}")

                # Mark the queued run as failed to prevent infinite retry loops
                try:
                    await db_client.update_queued_run(
                        queued_run_id=queued_run.id,
                        state="failed",
                        processed_at=datetime.now(UTC),
                    )
                    processed_run_ids.add(queued_run.id)
                    logger.info(
                        f"Marked queued run {queued_run.id} as failed due to error: {e}"
                    )
                except Exception as update_error:
                    logger.error(
                        f"Failed to mark queued run {queued_run.id} as failed: "
                        f"{update_error}"
                    )

        return processed_count

    async def _resolve_messaging_resources(self, campaign):
        """Resolve the (configuration, template, sending address) triple for
        a WhatsApp campaign, org-scoped. Raises ValueError when any piece is
        missing or unusable so the batch fails loudly instead of silently
        dropping rows."""
        if not campaign.messaging_configuration_id:
            raise ValueError(
                f"Campaign {campaign.id} has no messaging_configuration_id"
            )
        config = await db_client.get_messaging_configuration(
            campaign.messaging_configuration_id,
            organization_id=campaign.organization_id,
        )
        if not config or not config.is_active:
            raise ValueError(
                f"Messaging configuration {campaign.messaging_configuration_id} "
                f"not found or inactive for campaign {campaign.id}"
            )

        if not campaign.whatsapp_template_id:
            raise ValueError(f"Campaign {campaign.id} has no whatsapp_template_id")
        template = await db_client.get_whatsapp_template(
            campaign.whatsapp_template_id,
            organization_id=campaign.organization_id,
        )
        if not template:
            raise ValueError(
                f"WhatsApp template {campaign.whatsapp_template_id} not found "
                f"for campaign {campaign.id}"
            )
        if template.status != "APPROVED":
            raise ValueError(
                f"WhatsApp template {template.id} is not APPROVED "
                f"(status: {template.status}) for campaign {campaign.id}"
            )

        # The first active address of the configuration is the sender; its
        # external_id is the Meta phone_number_id used for the send.
        address = next(
            (a for a in (config.addresses or []) if a.is_active),
            None,
        )
        if not address:
            raise ValueError(
                f"Messaging configuration {config.id} has no active address "
                f"for campaign {campaign.id}"
            )

        return config, template, address

    async def dispatch_message(
        self,
        queued_run: QueuedRunModel,
        campaign,
        *,
        config,
        template,
        address,
        marketing: bool,
    ) -> Optional[WorkflowRunModel]:
        """Create a workflow run and send the campaign template to one
        recipient.

        Returns the workflow run on success, or None when the recipient was
        suppressed or the send failed with a classified WhatsApp API error
        (both handled terminally in here). Raises on unexpected errors so
        the batch loop can mark the queued run failed.
        """
        from api.services.messaging.whatsapp.client import (
            WhatsAppApiError,
            client_for_configuration,
        )
        from api.services.messaging.whatsapp.template_service import (
            build_send_components,
            render_template_display_text,
        )

        context_variables = queued_run.context_variables or {}
        phone_number = context_variables.get("phone_number")
        if not phone_number:
            raise ValueError(f"No phone number in queued run {queued_run.id}")

        wa_id = normalize_wa_id(phone_number)
        if not wa_id:
            raise ValueError(
                f"Invalid phone number in queued run {queued_run.id}: {phone_number}"
            )

        # Do-not-message enforcement before any run/quota bookkeeping.
        if await db_client.is_messaging_suppressed(
            organization_id=campaign.organization_id,
            address=wa_id,
            marketing=marketing,
        ):
            logger.info(
                f"Recipient suppressed for campaign {campaign.id} "
                f"(queued run {queued_run.id}); skipping send"
            )
            await db_client.update_queued_run(
                queued_run_id=queued_run.id,
                state="failed",
                retry_reason="suppressed",
                processed_at=datetime.now(UTC),
            )
            return None

        initial_context = {
            **context_variables,
            "campaign_id": campaign.id,
            "provider": "whatsapp",
            "source_uuid": queued_run.source_uuid,
            "wa_id": wa_id,
            "phone_number_id": address.external_id,
        }

        workflow_run_name = f"WR-CAMPAIGN-{campaign.id}-{queued_run.id}"
        workflow_run = await db_client.create_workflow_run(
            name=workflow_run_name,
            workflow_id=campaign.workflow_id,
            mode=WorkflowRunMode.WHATSAPP.value,
            user_id=campaign.created_by,
            call_type=CallType.OUTBOUND,
            initial_context=initial_context,
            campaign_id=campaign.id,
            queued_run_id=queued_run.id,  # Link to queued run for retry tracking
            organization_id=campaign.organization_id,
        )

        # Add "retry" tag if this is a retry send
        if context_variables.get("is_retry"):
            retry_reason = context_variables.get("retry_reason", "unknown")
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                gathered_context={
                    "call_tags": ["retry", f"retry_reason_{retry_reason}"]
                },
            )

        quota_result = await authorize_workflow_run_start(
            workflow_id=campaign.workflow_id,
            organization_id=campaign.organization_id,
            workflow_run_id=workflow_run.id,
        )
        if not quota_result.has_quota:
            error_message = quota_result.error_message or "Quota exceeded"
            logger.warning(
                f"Campaign {campaign.id} quota check failed for workflow run "
                f"{workflow_run.id}: {error_message}"
            )
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                is_completed=True,
                state=WorkflowRunState.COMPLETED.value,
                gathered_context={"error": error_message},
            )
            raise ValueError(error_message)

        subscription_check = await check_whatsapp_message_allowed(
            campaign.organization_id
        )
        if not subscription_check.allowed:
            await self._halt_on_subscription_block(
                subscription_check,
                queued_run=queued_run,
                campaign=campaign,
                workflow_run=workflow_run,
            )
            return None

        values = lowercase_template_values(context_variables)
        try:
            components = build_send_components(
                template.components, template.parameter_format, values=values
            )
        except ValueError as e:
            await self._fail_workflow_run(
                workflow_run, error=f"Template values missing: {e}"
            )
            raise

        client = client_for_configuration(config, address.external_id)
        try:
            wamid = await client.send_template(
                to=wa_id,
                name=template.name,
                language=template.language,
                components=components,
            )
        except WhatsAppApiError as e:
            await self._handle_send_api_error(
                e,
                queued_run=queued_run,
                campaign=campaign,
                workflow_run=workflow_run,
                wa_id=wa_id,
            )
            return None
        except Exception as e:
            logger.error(
                f"Failed to send WhatsApp template for workflow run "
                f"{workflow_run.id}: {e}"
            )
            await self._fail_workflow_run(workflow_run, error=str(e))

            # Record send failure in circuit breaker (mirrors telephony
            # call-initiation failures feeding the breaker).
            await circuit_breaker.record_and_evaluate(
                campaign.id,
                is_failure=True,
                workflow_run_id=workflow_run.id,
                reason="whatsapp_send_failed",
            )
            raise

        now = datetime.now(UTC)

        # The wamid is the message correlation id: stored as call_id so
        # status callbacks resolve the run via get_workflow_run_by_call_id.
        # template_text is the rendered message as delivered — the inbox
        # thread displays it instead of the template's technical name.
        await db_client.update_workflow_run(
            run_id=workflow_run.id,
            gathered_context={
                "call_id": wamid,
                "provider": "whatsapp",
                "template_text": render_template_display_text(
                    template.components, template.parameter_format, values
                ),
            },
        )

        try:
            conversation = await db_client.create_whatsapp_conversation(
                organization_id=campaign.organization_id,
                messaging_address_id=address.id,
                phone_number_id=address.external_id,
                wa_id=wa_id,
                workflow_run_id=workflow_run.id,
                campaign_id=campaign.id,
                service_window_expires_at=None,
            )
            await db_client.touch_whatsapp_conversation(
                conversation.id, last_outbound_at=now
            )
        except IntegrityError:
            # An open conversation already exists for this
            # (phone_number_id, wa_id) — inbound replies keep flowing to the
            # existing run; skipping the attach is acceptable.
            logger.info(
                f"Open WhatsApp conversation already exists for "
                f"phone_number_id={address.external_id} wa_id={wa_id}; "
                f"skipping conversation attach for run {workflow_run.id}"
            )

        logger.info(
            f"WhatsApp template '{template.name}' sent for workflow run "
            f"{workflow_run.id}, wamid: {wamid}"
        )
        return workflow_run

    async def _handle_send_api_error(
        self,
        error,
        *,
        queued_run: QueuedRunModel,
        campaign,
        workflow_run: WorkflowRunModel,
        wa_id: str,
    ) -> None:
        """Terminal handling for a classified WhatsApp API send failure."""
        retry_kind = getattr(error, "retry_kind", None)
        action = action_for_retry_kind(retry_kind)

        logger.warning(
            f"WhatsApp send failed for workflow run {workflow_run.id} "
            f"(campaign {campaign.id}, queued run {queued_run.id}): {error}; "
            f"code={getattr(error, 'code', None)} retry_kind={retry_kind} "
            f"action={action}"
        )

        await self._fail_workflow_run(
            workflow_run,
            error=str(error),
            extra_log={
                "code": getattr(error, "code", None),
                "subcode": getattr(error, "subcode", None),
                "http_status": getattr(error, "http_status", None),
                "retry_kind": retry_kind,
            },
        )

        await db_client.update_queued_run(
            queued_run_id=queued_run.id,
            state="failed",
            processed_at=datetime.now(UTC),
        )

        if action == ACTION_RETRY:
            # Transient failure — schedule a retry through the existing
            # campaign retry event path (orchestrator applies retry_config).
            publisher = await get_campaign_event_publisher()
            await publisher.publish_retry_needed(
                workflow_run_id=workflow_run.id,
                reason="whatsapp_backoff",
                campaign_id=campaign.id,
                queued_run_id=queued_run.id,
            )
        elif action == ACTION_SUPPRESS_24H:
            await db_client.upsert_messaging_suppression(
                organization_id=campaign.organization_id,
                address=wa_id,
                scope="marketing",
                reason="meta_131049",
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
        elif action == ACTION_SUPPRESS_PERMANENT:
            await db_client.upsert_messaging_suppression(
                organization_id=campaign.organization_id,
                address=wa_id,
                scope="marketing",
                reason="meta_131050",
                expires_at=None,
            )
        elif action == ACTION_HALT:
            # Systemic failure (e.g. account/payment issue) — feed the
            # circuit breaker so the campaign pauses instead of burning
            # through the whole list.
            await circuit_breaker.record_and_evaluate(
                campaign.id,
                is_failure=True,
                workflow_run_id=workflow_run.id,
                reason=f"whatsapp_{getattr(error, 'code', 'api_error')}",
            )
        # ACTION_FAIL: nothing beyond marking the run and queued run failed.

    async def _halt_on_subscription_block(
        self,
        check,
        *,
        queued_run: QueuedRunModel,
        campaign,
        workflow_run: WorkflowRunModel,
    ) -> None:
        """Terminal handling for a subscription/quota block on a send.

        A subscription block is systemic — every recipient of the campaign
        hits the same wall — so this mirrors the ACTION_HALT branch of
        ``_handle_send_api_error``: fail the run and the queued run, then
        feed the circuit breaker so the campaign pauses instead of burning
        through the remaining list.
        """
        logger.warning(
            f"WhatsApp send blocked by subscription for workflow run "
            f"{workflow_run.id} (campaign {campaign.id}, queued run "
            f"{queued_run.id}): {check.error_code} - {check.error_message}"
        )

        await self._fail_workflow_run(
            workflow_run,
            error=check.error_message,
            extra_log={"error_code": check.error_code},
        )

        await db_client.update_queued_run(
            queued_run_id=queued_run.id,
            state="failed",
            retry_reason=check.error_code,
            processed_at=datetime.now(UTC),
        )

        await circuit_breaker.record_and_evaluate(
            campaign.id,
            is_failure=True,
            workflow_run_id=workflow_run.id,
            reason=check.error_code,
        )

    async def _fail_workflow_run(
        self,
        workflow_run: WorkflowRunModel,
        *,
        error: str,
        extra_log: dict | None = None,
    ) -> None:
        """Mark a workflow run completed-with-error, appending the failure to
        the whatsapp status callback log (mirrors telephony failure logging).
        """
        callback_logs = (
            workflow_run.logs.get("whatsapp_status_callbacks", [])
            if workflow_run.logs
            else []
        )
        callback_logs.append(
            {
                "status": "failed",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {"error": error, **(extra_log or {})},
            }
        )
        await db_client.update_workflow_run(
            run_id=workflow_run.id,
            is_completed=True,
            state=WorkflowRunState.COMPLETED.value,
            gathered_context={"error": error},
            logs={"whatsapp_status_callbacks": callback_logs},
        )

    async def apply_rate_limit(self, phone_number_id: str, rate_limit: int) -> None:
        """Enforce the campaign's per-second send pace, keyed per business
        phone number (``wa:<phone_number_id>``), reusing the sliding-window
        rate limiter mechanics from the voice dispatcher."""
        rate_key = f"wa:{phone_number_id}"
        max_wait = 1.0  # Maximum time to wait for a slot
        start_time = time.time()

        while True:
            if await rate_limiter.acquire_token(rate_key, rate_limit):
                return  # Got permission to proceed

            wait_time = await rate_limiter.get_next_available_slot(
                rate_key, rate_limit
            )

            # Don't wait forever
            if time.time() - start_time + wait_time > max_wait:
                raise TimeoutError("Rate limit timeout - try again later")

            await asyncio.sleep(wait_time)

    async def _return_unprocessed_claims(
        self,
        queued_runs: list[QueuedRunModel],
        processed_run_ids: set[int],
        *,
        reason: str,
    ) -> None:
        queued_run_ids = [
            queued_run.id
            for queued_run in queued_runs
            if queued_run.id not in processed_run_ids
        ]
        if not queued_run_ids:
            return

        try:
            returned_count = (
                await db_client.return_processing_queued_runs_without_workflow(
                    queued_run_ids
                )
            )
            logger.info(
                f"Returned {returned_count}/{len(queued_run_ids)} claimed queued "
                f"runs back to queued state; reason={reason}; "
                f"queued_run_ids={queued_run_ids}"
            )
        except Exception as revert_error:
            logger.error(
                f"Failed to return claimed queued runs; reason={reason}; "
                f"queued_run_ids={queued_run_ids}; error={revert_error}"
            )


async def handle_whatsapp_status_event(status: dict) -> None:
    """Process one raw entry from a WhatsApp webhook ``statuses`` array.

    Appends the status to the run's ``whatsapp_status_callbacks`` log
    (mirroring telephony status callbacks), upserts suppressions for Meta
    delivery-failure codes 131049/131050, and feeds the campaign circuit
    breaker on delivery outcomes.
    """
    wamid = status.get("id") if isinstance(status, dict) else None
    if not wamid:
        logger.warning("WhatsApp status event without message id; ignoring")
        return

    workflow_run = await db_client.get_workflow_run_by_call_id(wamid)
    if not workflow_run:
        logger.debug(f"No workflow run found for WhatsApp status wamid={wamid}")
        return

    status_value = str(status.get("status") or "").lower()

    callback_logs = (
        workflow_run.logs.get("whatsapp_status_callbacks", [])
        if workflow_run.logs
        else []
    )
    callback_logs.append(
        {
            "status": status_value,
            "timestamp": datetime.now(UTC).isoformat(),
            "wamid": wamid,
            "data": status,
        }
    )
    await db_client.update_workflow_run(
        run_id=workflow_run.id,
        logs={"whatsapp_status_callbacks": callback_logs},
    )

    if status_value == "delivered":
        if workflow_run.campaign_id:
            await circuit_breaker.record_and_evaluate(
                workflow_run.campaign_id, is_failure=False
            )
        return

    if status_value != "failed":
        return

    logger.warning(
        f"[run {workflow_run.id}] WhatsApp message {wamid} failed: "
        f"{status.get('errors')}"
    )

    errors = [err for err in (status.get("errors") or []) if isinstance(err, dict)]
    suppressions = [
        supp
        for supp in (
            suppression_for_meta_error_code(err.get("code")) for err in errors
        )
        if supp
    ]
    if suppressions:
        organization_id = await db_client.get_organization_id_by_workflow_run_id(
            workflow_run.id
        )
        initial_context = workflow_run.initial_context or {}
        recipient = normalize_wa_id(
            status.get("recipient_id")
            or initial_context.get("wa_id")
            or initial_context.get("phone_number")
        )
        if organization_id and recipient:
            now = datetime.now(UTC)
            for scope, reason, ttl in suppressions:
                await db_client.upsert_messaging_suppression(
                    organization_id=organization_id,
                    address=recipient,
                    scope=scope,
                    reason=reason,
                    expires_at=now + ttl if ttl else None,
                )
                logger.info(
                    f"[run {workflow_run.id}] Upserted messaging suppression "
                    f"for {recipient}: scope={scope} reason={reason}"
                )
        else:
            logger.warning(
                f"[run {workflow_run.id}] Could not upsert suppression for "
                f"failed message {wamid}: organization_id={organization_id} "
                f"recipient={recipient!r}"
            )

    if workflow_run.campaign_id:
        error_codes = [err.get("code") for err in errors if err.get("code")]
        await circuit_breaker.record_and_evaluate(
            workflow_run.campaign_id,
            is_failure=True,
            workflow_run_id=workflow_run.id,
            reason=(
                f"whatsapp_failed_{error_codes[0]}"
                if error_codes
                else "whatsapp_failed"
            ),
        )


# Global instance
campaign_message_dispatcher = CampaignMessageDispatcher()
