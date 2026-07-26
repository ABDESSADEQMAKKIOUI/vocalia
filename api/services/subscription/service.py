"""Domain logic of the SaaS subscription layer.

Owns the lifecycle of an organization subscription (provisioning, plan
changes, suspension, cancellation, renewal, per-tenant limit overrides), the
resolution of the effective limits a tenant is held to, and the usage snapshot
those limits are compared against. Every mutation appends a row to
``subscription_events`` so the platform admin has a full audit trail.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta
from loguru import logger

from api.db import db_client
from api.db.models import (
    OrganizationModel,
    OrganizationSubscriptionModel,
    SubscriptionEventModel,
    SubscriptionPlanModel,
    UserModel,
)
from api.services.subscription.plans import DEFAULT_CURRENCY, FEATURE_KEYS
from api.utils.auth import hash_password

# Canonical shape of the EffectiveLimits dict. NULL/None means unlimited.
LIMIT_KEYS = (
    "max_voice_minutes",
    "max_whatsapp_messages",
    "max_workflows",
    "max_campaigns_per_month",
    "max_users",
    "max_concurrent_calls",
)


@dataclass
class ProvisionedOrganization:
    """Everything created by a single platform-admin provisioning call."""

    organization: OrganizationModel
    owner: UserModel
    subscription: OrganizationSubscriptionModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _advance(moment: datetime, billing_interval: str) -> datetime:
    """Move a datetime forward by one billing interval."""
    if billing_interval == "yearly":
        return moment + relativedelta(years=1)
    return moment + relativedelta(months=1)


def _calendar_month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, start + relativedelta(months=1)


def get_period_bounds(
    subscription: OrganizationSubscriptionModel | None,
) -> tuple[datetime, datetime]:
    """Resolve the window usage is measured over.

    Organizations without a subscription (existing OSS tenants) fall back to
    the current calendar month so usage is still reportable.
    """
    if subscription is not None:
        return subscription.current_period_start, subscription.current_period_end
    return _calendar_month_bounds(_utcnow())


async def get_subscription(
    organization_id: int,
) -> OrganizationSubscriptionModel | None:
    """Get the subscription of an organization, with its plan loaded."""
    return await db_client.get_organization_subscription(organization_id)


def get_effective_limits(
    subscription: OrganizationSubscriptionModel | None,
    plan: SubscriptionPlanModel | None,
) -> dict[str, int | None]:
    """Merge the plan ceilings with the per-tenant overrides.

    A key present in ``limit_overrides`` always wins, including when its value
    is ``None``: that spells "unlimited for this tenant" and must not be
    confused with an absent key, which falls back to the plan.
    """
    limits: dict[str, int | None] = {
        key: getattr(plan, key, None) for key in LIMIT_KEYS
    }
    overrides = (subscription.limit_overrides or {}) if subscription else {}
    for key in LIMIT_KEYS:
        if key in overrides:
            limits[key] = overrides[key]
    return limits


def is_feature_enabled(plan: SubscriptionPlanModel | None, feature: str) -> bool:
    """Check a feature flag on a plan.

    A key absent from the ``features`` map is treated as enabled: only an
    explicit ``false`` gates a feature off. A partially configured plan must
    not silently cut production traffic, which is the same fail-open stance the
    enforcement layer takes.
    """
    features = (getattr(plan, "features", None) or {}) if plan else {}
    return bool(features.get(feature, True))


def get_features(plan: SubscriptionPlanModel | None) -> dict[str, bool]:
    """Resolve every known feature flag of a plan into a complete map."""
    return {key: is_feature_enabled(plan, key) for key in FEATURE_KEYS}


async def get_usage_snapshot(organization_id: int) -> dict:
    """Aggregate the consumption of an organization over its current period."""
    subscription = await db_client.get_organization_subscription(organization_id)
    period_start, period_end = get_period_bounds(subscription)

    voice_seconds = await db_client.get_voice_seconds_for_organization(
        organization_id, since=period_start, until=period_end
    )
    whatsapp_messages = await db_client.count_whatsapp_messages_for_organization(
        organization_id, since=period_start, until=period_end
    )
    workflows = await db_client.count_workflows_for_organization(organization_id)
    campaigns = await db_client.count_campaigns_for_organization(
        organization_id, since=period_start
    )
    users = await db_client.count_users_for_organization(organization_id)

    return {
        "voice_minutes": round(voice_seconds / 60, 2),
        "whatsapp_messages": whatsapp_messages,
        "workflows": workflows,
        "campaigns_this_period": campaigns,
        "users": users,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }


async def record_event(
    organization_id: int,
    event_type: str,
    *,
    subscription_id: int | None = None,
    actor_user_id: int | None = None,
    payload: dict | None = None,
    note: str | None = None,
) -> SubscriptionEventModel:
    """Append an entry to the subscription audit journal."""
    return await db_client.create_subscription_event(
        organization_id=organization_id,
        event_type=event_type,
        subscription_id=subscription_id,
        actor_user_id=actor_user_id,
        payload=payload,
        note=note,
    )


async def _require_plan(plan_code: str) -> SubscriptionPlanModel:
    plan = await db_client.get_subscription_plan_by_code(plan_code)
    if plan is None:
        raise ValueError(f"Unknown plan code: {plan_code}")
    if not plan.is_active:
        raise ValueError(f"Plan {plan_code} is not active")
    return plan


async def _require_subscription(
    organization_id: int,
) -> OrganizationSubscriptionModel:
    subscription = await db_client.get_organization_subscription(organization_id)
    if subscription is None:
        raise ValueError(f"Organization {organization_id} has no subscription")
    return subscription


async def _apply(organization_id: int, **fields) -> OrganizationSubscriptionModel:
    """Persist subscription fields and return the row with its plan loaded.

    The upsert refreshes columns only, so reading ``.plan`` off its result
    would lazy-load on a closed session. Re-reading through the joined-load
    accessor keeps what the mutators hand back safe to serialize.
    """
    await db_client.upsert_organization_subscription(organization_id, **fields)
    return await _require_subscription(organization_id)


def _sanitize_overrides(limit_overrides: dict | None) -> dict:
    """Keep only known limit keys and coerce their values to int or None."""
    sanitized: dict[str, int | None] = {}
    for key, value in (limit_overrides or {}).items():
        if key not in LIMIT_KEYS:
            continue
        if value is None:
            sanitized[key] = None
            continue
        try:
            sanitized[key] = int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"Invalid value for limit override {key}: {value}"
            ) from None
    return sanitized


async def provision_organization(
    *,
    name: str,
    contact_email: str,
    owner_email: str,
    owner_password: str,
    plan_code: str,
    trial_days: int | None = None,
    notes: str | None = None,
    actor_user_id: int | None = None,
) -> ProvisionedOrganization:
    """Create a tenant: organization, owner user and subscription.

    ``trial_days`` overrides the plan default; a non-zero trial starts the
    subscription in ``trialing``, otherwise it starts ``active``. The billing
    period always runs from now to one billing interval later, independently of
    the trial window. Raises ``ValueError`` when the owner email is taken or
    the plan code is unknown or inactive.
    """
    plan = await _require_plan(plan_code)

    existing_user = await db_client.get_user_by_email(owner_email)
    if existing_user:
        raise ValueError(f"Email already registered: {owner_email}")

    owner = await db_client.create_user_with_email(
        email=owner_email,
        password_hash=hash_password(owner_password),
    )

    org_provider_id = f"org_saas_{uuid.uuid4().hex}"
    organization, _ = await db_client.get_or_create_organization_by_provider_id(
        org_provider_id=org_provider_id, user_id=owner.id
    )
    await db_client.add_user_to_organization(owner.id, organization.id)
    await db_client.update_user_selected_organization(owner.id, organization.id)

    organization = (
        await db_client.update_organization_profile(
            organization.id, name=name, contact_email=contact_email
        )
        or organization
    )

    now = _utcnow()
    effective_trial_days = plan.trial_days if trial_days is None else trial_days
    subscription = await _apply(
        organization.id,
        plan_id=plan.id,
        status="trialing" if effective_trial_days > 0 else "active",
        current_period_start=now,
        current_period_end=_advance(now, plan.billing_interval),
        trial_end_at=(
            now + timedelta(days=effective_trial_days)
            if effective_trial_days > 0
            else None
        ),
        limit_overrides={},
        notes=notes,
        created_by_user_id=actor_user_id,
    )

    await record_event(
        organization.id,
        "provisioned",
        subscription_id=subscription.id,
        actor_user_id=actor_user_id,
        payload={
            "plan_code": plan.code,
            "owner_email": owner.email,
            "trial_days": effective_trial_days,
        },
        note=notes,
    )
    logger.info(
        f"Provisioned organization {organization.id} ({name}) on plan {plan.code}"
    )
    return ProvisionedOrganization(
        organization=organization, owner=owner, subscription=subscription
    )


async def assign_plan(
    organization_id: int,
    plan_code: str,
    *,
    status: str | None = None,
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
    limit_overrides: dict | None = None,
    notes: str | None = None,
    actor_user_id: int | None = None,
) -> OrganizationSubscriptionModel:
    """Attach an organization to a plan, creating the subscription if needed.

    The existing period and status are preserved unless explicitly overridden,
    so moving a tenant between plans mid-period does not reset its billing
    window. A brand new subscription starts now for one billing interval.
    Raises ``ValueError`` when the plan code is unknown or inactive.
    """
    plan = await _require_plan(plan_code)
    existing = await db_client.get_organization_subscription(organization_id)
    now = _utcnow()

    if current_period_start is None:
        current_period_start = existing.current_period_start if existing else now
    if current_period_end is None:
        current_period_end = (
            existing.current_period_end
            if existing
            else _advance(now, plan.billing_interval)
        )
    if status is None:
        status = (
            existing.status
            if existing
            else ("trialing" if plan.trial_days > 0 else "active")
        )

    fields = {
        "plan_id": plan.id,
        "status": status,
        "current_period_start": current_period_start,
        "current_period_end": current_period_end,
    }
    if limit_overrides is not None:
        fields["limit_overrides"] = _sanitize_overrides(limit_overrides)
    if notes is not None:
        fields["notes"] = notes
    if existing is None:
        fields["created_by_user_id"] = actor_user_id

    subscription = await _apply(organization_id, **fields)
    await record_event(
        organization_id,
        "plan_changed",
        subscription_id=subscription.id,
        actor_user_id=actor_user_id,
        payload={
            "from_plan_id": existing.plan_id if existing else None,
            "to_plan_code": plan.code,
            "status": status,
        },
        note=notes,
    )
    return subscription


async def suspend(
    organization_id: int,
    *,
    reason: str | None = None,
    actor_user_id: int | None = None,
) -> OrganizationSubscriptionModel:
    """Suspend a tenant: every enforced check denies until reactivation."""
    await _require_subscription(organization_id)
    subscription = await _apply(organization_id, status="suspended")
    await record_event(
        organization_id,
        "suspended",
        subscription_id=subscription.id,
        actor_user_id=actor_user_id,
        note=reason,
    )
    logger.info(f"Suspended subscription of organization {organization_id}")
    return subscription


async def reactivate(
    organization_id: int, *, actor_user_id: int | None = None
) -> OrganizationSubscriptionModel:
    """Lift a suspension or a pending cancellation.

    The tenant returns to ``trialing`` while its trial window is still open,
    otherwise to ``active``.
    """
    existing = await _require_subscription(organization_id)
    trial_end_at = existing.trial_end_at
    status = "trialing" if trial_end_at and trial_end_at > _utcnow() else "active"

    subscription = await _apply(
        organization_id, status=status, cancel_at_period_end=False
    )
    await record_event(
        organization_id,
        "reactivated",
        subscription_id=subscription.id,
        actor_user_id=actor_user_id,
        payload={"status": status},
    )
    logger.info(f"Reactivated subscription of organization {organization_id}")
    return subscription


async def cancel(
    organization_id: int,
    *,
    at_period_end: bool = True,
    reason: str | None = None,
    actor_user_id: int | None = None,
) -> OrganizationSubscriptionModel:
    """Cancel a subscription, at the end of the period or immediately."""
    await _require_subscription(organization_id)
    fields = (
        {"cancel_at_period_end": True}
        if at_period_end
        else {"status": "cancelled", "cancel_at_period_end": False}
    )
    subscription = await _apply(organization_id, **fields)
    await record_event(
        organization_id,
        "cancelled",
        subscription_id=subscription.id,
        actor_user_id=actor_user_id,
        payload={"at_period_end": at_period_end},
        note=reason,
    )
    logger.info(
        f"Cancelled subscription of organization {organization_id} "
        f"(at_period_end={at_period_end})"
    )
    return subscription


async def renew_period(
    organization_id: int, *, actor_user_id: int | None = None
) -> OrganizationSubscriptionModel:
    """Roll the billing window forward by one interval.

    The new period starts where the previous one ended, so consecutive renewals
    never drift. A subscription flagged ``cancel_at_period_end`` terminates here
    instead of being renewed, and a trial that has run out becomes ``active``.
    Never called implicitly by the enforcement checks: an expired period stays
    expired until the platform admin renews it.

    When several intervals have already elapsed, the window is advanced until it
    covers the present. Rolling a single interval forward would otherwise land
    entirely in the past and leave the tenant blocked on ``subscription_expired``
    with no visible reason.
    """
    existing = await _require_subscription(organization_id)
    plan = existing.plan
    billing_interval = plan.billing_interval if plan else "monthly"

    new_start = existing.current_period_end
    new_end = _advance(new_start, billing_interval)
    now = _utcnow()
    # Bounded: _advance always moves forward by at least a month.
    while new_end <= now:
        new_start = new_end
        new_end = _advance(new_start, billing_interval)
    ends_now = bool(existing.cancel_at_period_end)

    fields: dict = {
        "current_period_start": new_start,
        "current_period_end": new_end,
    }
    if ends_now:
        fields["status"] = "cancelled"
    elif existing.status == "trialing" and (
        existing.trial_end_at is None or existing.trial_end_at <= new_start
    ):
        fields["status"] = "active"

    subscription = await _apply(organization_id, **fields)
    await record_event(
        organization_id,
        "renewed",
        subscription_id=subscription.id,
        actor_user_id=actor_user_id,
        payload={
            "current_period_start": new_start.isoformat(),
            "current_period_end": new_end.isoformat(),
            "status": subscription.status,
        },
    )
    if ends_now:
        await record_event(
            organization_id,
            "cancelled",
            subscription_id=subscription.id,
            actor_user_id=actor_user_id,
            payload={"at_period_end": True},
        )
    return subscription


async def update_limits(
    organization_id: int,
    limit_overrides: dict | None,
    *,
    actor_user_id: int | None = None,
) -> OrganizationSubscriptionModel:
    """Replace the per-tenant limit overrides.

    Unknown keys are dropped; a key mapped to ``None`` means unlimited for this
    tenant and wins over the plan ceiling.
    """
    await _require_subscription(organization_id)
    sanitized = _sanitize_overrides(limit_overrides)

    subscription = await _apply(organization_id, limit_overrides=sanitized)
    await record_event(
        organization_id,
        "limits_updated",
        subscription_id=subscription.id,
        actor_user_id=actor_user_id,
        payload={"limit_overrides": sanitized},
    )
    return subscription


async def get_platform_metrics() -> dict:
    """Aggregate the platform-wide subscription and usage figures.

    ``mrr_amount`` normalizes yearly plans to a monthly amount and counts only
    the subscriptions that are billable today (``active`` and ``trialing``).
    Amounts are summed without conversion, so the reported currency is the one
    of the counted plans; the catalogue is single-currency by design.
    """
    total_organizations = await db_client.count_organizations()
    status_counts = await db_client.count_subscriptions_by_status()
    subscriptions = await db_client.list_active_subscriptions_with_plans()

    mrr_amount = 0.0
    currency = DEFAULT_CURRENCY
    for subscription, plan in subscriptions:
        if subscription.status not in ("active", "trialing"):
            continue
        price = plan.price_amount or 0
        mrr_amount += price / 12 if plan.billing_interval == "yearly" else price
        currency = plan.currency or currency

    period_start, period_end = _calendar_month_bounds(_utcnow())
    voice_seconds, whatsapp_messages = await db_client.get_platform_usage_totals(
        since=period_start, until=period_end
    )

    return {
        "total_organizations": total_organizations,
        "active_subscriptions": status_counts.get("active", 0),
        "trialing": status_counts.get("trialing", 0),
        "suspended": status_counts.get("suspended", 0),
        "past_due": status_counts.get("past_due", 0),
        "mrr_amount": round(mrr_amount, 2),
        "currency": currency,
        "voice_minutes_this_period": round(voice_seconds / 60, 2),
        "whatsapp_messages_this_period": whatsapp_messages,
    }
