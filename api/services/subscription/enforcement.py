"""Runtime gate between a tenant's subscription and the billable runtimes.

Every check follows the same contract:

- enforcement disabled by env, or the organization has no subscription row
  (existing OSS tenants) -> allow, so nothing that worked before this layer
  existed can start failing because of it;
- ``suspended``/``cancelled`` status, elapsed period, feature not in the plan,
  or a reached quota -> deny with a stable ``error_code``;
- any unexpected error -> allow. This is a deliberate fail-open: an incident in
  the billing layer (database unavailable, malformed limits, …) must never cut
  a customer's voice production. Metering keeps running, so the overage is
  still visible to the platform admin afterwards.

Each denial is journaled as a ``quota_blocked`` subscription event on a
best-effort basis: journaling can never turn into a second failure mode.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from api.constants import SUBSCRIPTION_ENFORCEMENT_ENABLED
from api.db import db_client
from api.db.models import OrganizationSubscriptionModel
from api.enums import WorkflowRunMode
from api.services.subscription.service import (
    get_effective_limits,
    is_feature_enabled,
    record_event,
)

# Text-only run modes: gated on the whatsapp feature, never on voice minutes.
NON_VOICE_RUN_MODES = frozenset(
    {
        WorkflowRunMode.WHATSAPP.value,
        WorkflowRunMode.TEXTCHAT.value,
        WorkflowRunMode.CHAT.value,
    }
)

SUSPENDED_MESSAGE = "Abonnement suspendu. Contactez votre administrateur."
CANCELLED_MESSAGE = (
    "Abonnement résilié. Contactez votre administrateur pour le réactiver."
)
EXPIRED_MESSAGE = (
    "Votre période d'abonnement est arrivée à échéance. "
    "Contactez votre administrateur pour la renouveler."
)
VOICE_QUOTA_MESSAGE = "Quota de minutes vocales atteint pour la période en cours."
WHATSAPP_QUOTA_MESSAGE = (
    "Quota de conversations WhatsApp atteint pour la période en cours."
)
CAMPAIGN_QUOTA_MESSAGE = "Quota de campagnes atteint pour la période en cours."

# Customer-facing labels used to build the feature_not_in_plan message.
FEATURE_LABELS = {
    "voice": "Appels vocaux",
    "whatsapp": "WhatsApp",
    "campaigns": "Campagnes",
    "telephony": "Téléphonie",
    "api_access": "Accès API",
    "knowledge_base": "Base de connaissances",
    "integrations": "Intégrations",
}


@dataclass
class SubscriptionCheck:
    """Outcome of a subscription check, mirroring QuotaCheckResult."""

    allowed: bool
    error_code: str = ""
    error_message: str = ""


def _allow() -> SubscriptionCheck:
    return SubscriptionCheck(allowed=True)


def _deny(error_code: str, error_message: str) -> SubscriptionCheck:
    return SubscriptionCheck(
        allowed=False, error_code=error_code, error_message=error_message
    )


def _feature_denied(feature: str) -> SubscriptionCheck:
    label = FEATURE_LABELS.get(feature, feature)
    return _deny(
        "feature_not_in_plan",
        f"La fonctionnalité « {label} » n'est pas incluse dans votre abonnement.",
    )


def _check_status(
    subscription: OrganizationSubscriptionModel,
) -> SubscriptionCheck:
    """Gate on the subscription status and the billing period."""
    if subscription.status == "suspended":
        return _deny("subscription_suspended", SUSPENDED_MESSAGE)
    if subscription.status == "cancelled":
        return _deny("subscription_cancelled", CANCELLED_MESSAGE)
    if subscription.current_period_end <= datetime.now(timezone.utc):
        return _deny("subscription_expired", EXPIRED_MESSAGE)
    return _allow()


async def _record_block(
    organization_id: int,
    subscription: OrganizationSubscriptionModel,
    check: SubscriptionCheck,
    payload: dict,
) -> None:
    """Journal a denial. Best-effort: never raises, never blocks the caller."""
    try:
        await record_event(
            organization_id,
            "quota_blocked",
            subscription_id=subscription.id,
            payload={"error_code": check.error_code, **payload},
        )
    except Exception as e:
        logger.warning(
            f"Failed to record quota_blocked event for org {organization_id}: {e}"
        )


async def check_run_allowed(
    organization_id: int, run_mode: str | None = None
) -> SubscriptionCheck:
    """Authorize a workflow run: status, then channel feature and quota.

    ``run_mode`` is the WorkflowRunMode of the run being started. Text-only
    modes (text chat, WhatsApp) are gated on the ``whatsapp`` feature and never
    on the voice minutes quota — otherwise a plan whose voice minutes are spent
    would also silence its WhatsApp conversations, and a WhatsApp-only plan
    (voice disabled) could never run at all. ``None`` means voice, which is the
    historical default for callers that do not know their mode.
    """
    if not SUBSCRIPTION_ENFORCEMENT_ENABLED:
        return _allow()

    is_voice_run = run_mode is None or run_mode not in NON_VOICE_RUN_MODES

    try:
        subscription = await db_client.get_organization_subscription(organization_id)
        if subscription is None:
            return _allow()

        check = _check_status(subscription)
        if check.allowed:
            required_feature = "voice" if is_voice_run else "whatsapp"
            if not is_feature_enabled(subscription.plan, required_feature):
                check = _feature_denied(required_feature)

        used_minutes = None
        if check.allowed and is_voice_run:
            limits = get_effective_limits(subscription, subscription.plan)
            max_voice_minutes = limits["max_voice_minutes"]
            if max_voice_minutes is not None:
                voice_seconds = await db_client.get_voice_seconds_for_organization(
                    organization_id,
                    since=subscription.current_period_start,
                    until=subscription.current_period_end,
                )
                used_minutes = round(voice_seconds / 60, 2)
                if used_minutes >= max_voice_minutes:
                    check = _deny("voice_quota_exceeded", VOICE_QUOTA_MESSAGE)

        if not check.allowed:
            logger.warning(
                f"Subscription check denied run for org {organization_id}: "
                f"{check.error_code}"
            )
            await _record_block(
                organization_id,
                subscription,
                check,
                {"check": "run", "used_voice_minutes": used_minutes},
            )
        return check
    except Exception as e:
        logger.warning(
            f"Subscription check failed for org {organization_id}, allowing run: {e}"
        )
        return _allow()


async def check_campaign_allowed(organization_id: int) -> SubscriptionCheck:
    """Authorize a new campaign: status, campaigns feature, then the period cap.

    Counted over the subscription period rather than the calendar month, so the
    cap lines up with the window the customer is billed for.
    """
    if not SUBSCRIPTION_ENFORCEMENT_ENABLED:
        return _allow()

    try:
        subscription = await db_client.get_organization_subscription(organization_id)
        if subscription is None:
            return _allow()

        check = _check_status(subscription)
        if check.allowed and not is_feature_enabled(subscription.plan, "campaigns"):
            check = _feature_denied("campaigns")

        used_campaigns = None
        if check.allowed:
            limits = get_effective_limits(subscription, subscription.plan)
            max_campaigns = limits["max_campaigns_per_month"]
            if max_campaigns is not None:
                used_campaigns = await db_client.count_campaigns_for_organization(
                    organization_id,
                    since=subscription.current_period_start,
                )
                if used_campaigns >= max_campaigns:
                    check = _deny("campaign_quota_exceeded", CAMPAIGN_QUOTA_MESSAGE)

        if not check.allowed:
            logger.warning(
                f"Subscription check denied campaign creation for org "
                f"{organization_id}: {check.error_code}"
            )
            await _record_block(
                organization_id,
                subscription,
                check,
                {"check": "campaign", "used_campaigns": used_campaigns},
            )
        return check
    except Exception as e:
        logger.warning(
            f"Subscription campaign check failed for org {organization_id}, "
            f"allowing campaign: {e}"
        )
        return _allow()


async def check_feature_allowed(
    organization_id: int, feature: str
) -> SubscriptionCheck:
    """Authorize the use of a plan feature: status, then the feature flag."""
    if not SUBSCRIPTION_ENFORCEMENT_ENABLED:
        return _allow()

    try:
        subscription = await db_client.get_organization_subscription(organization_id)
        if subscription is None:
            return _allow()

        check = _check_status(subscription)
        if check.allowed and not is_feature_enabled(subscription.plan, feature):
            check = _feature_denied(feature)

        if not check.allowed:
            logger.warning(
                f"Subscription check denied feature {feature} for org "
                f"{organization_id}: {check.error_code}"
            )
            await _record_block(
                organization_id,
                subscription,
                check,
                {"check": "feature", "feature": feature},
            )
        return check
    except Exception as e:
        logger.warning(
            f"Subscription feature check failed for org {organization_id}, "
            f"allowing {feature}: {e}"
        )
        return _allow()


async def check_whatsapp_message_allowed(
    organization_id: int, count: int = 1
) -> SubscriptionCheck:
    """Authorize outbound WhatsApp units: status, feature, messages quota."""
    if not SUBSCRIPTION_ENFORCEMENT_ENABLED:
        return _allow()

    try:
        subscription = await db_client.get_organization_subscription(organization_id)
        if subscription is None:
            return _allow()

        check = _check_status(subscription)
        if check.allowed and not is_feature_enabled(subscription.plan, "whatsapp"):
            check = _feature_denied("whatsapp")

        used_messages = None
        if check.allowed:
            limits = get_effective_limits(subscription, subscription.plan)
            max_whatsapp_messages = limits["max_whatsapp_messages"]
            if max_whatsapp_messages is not None:
                used_messages = (
                    await db_client.count_whatsapp_messages_for_organization(
                        organization_id,
                        since=subscription.current_period_start,
                        until=subscription.current_period_end,
                    )
                )
                if used_messages + count > max_whatsapp_messages:
                    check = _deny("whatsapp_quota_exceeded", WHATSAPP_QUOTA_MESSAGE)

        if not check.allowed:
            logger.warning(
                f"Subscription check denied WhatsApp send for org "
                f"{organization_id}: {check.error_code}"
            )
            await _record_block(
                organization_id,
                subscription,
                check,
                {
                    "check": "whatsapp",
                    "count": count,
                    "used_whatsapp_messages": used_messages,
                },
            )
        return check
    except Exception as e:
        logger.warning(
            f"Subscription WhatsApp check failed for org {organization_id}, "
            f"allowing send: {e}"
        )
        return _allow()
