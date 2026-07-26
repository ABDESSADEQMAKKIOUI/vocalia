"""Subscription and plan schemas.

Request/response bodies for the platform-admin endpoints in
``api/routes/platform_admin.py`` and for the tenant-facing
``GET /organizations/subscription`` in ``api/routes/organization.py``.

``EffectiveLimits`` and ``UsageSnapshot`` are the canonical shapes produced by
``api/services/subscription/service.py``; a NULL limit always means unlimited.
"""

import re
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from api.services.subscription.plans import FEATURE_KEYS

SubscriptionStatus = Literal[
    "trialing", "active", "past_due", "suspended", "cancelled"
]
BillingInterval = Literal["monthly", "yearly"]

_PLAN_CODE_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")


def _normalize_plan_code(value: str) -> str:
    """Lowercase and validate a plan code as a slug."""
    code = value.strip().lower()
    if not _PLAN_CODE_PATTERN.match(code):
        raise ValueError(
            "Plan code must be a lowercase slug of letters, digits, '-' or '_'"
        )
    return code


def _normalize_features(value: dict) -> dict:
    """Expand a features map to every known key, defaulting absent ones to off.

    ``is_feature_enabled`` treats a missing key as granted, so a partial map
    stored from a form that only sends the boxes it ticked would silently grant
    every feature the operator left alone. Normalizing on the way in makes the
    stored map exhaustive and the gating meaningful, whatever the client.
    """
    unknown = sorted(set(value) - set(FEATURE_KEYS))
    if unknown:
        raise ValueError(f"Unknown feature keys: {', '.join(unknown)}")
    return {key: bool(value.get(key, False)) for key in FEATURE_KEYS}


# ---------------------------------------------------------------------------
# Limits and usage
# ---------------------------------------------------------------------------


class EffectiveLimits(BaseModel):
    """Plan limits after the subscription's ``limit_overrides`` are applied.

    ``None`` on any field means unlimited.
    """

    max_voice_minutes: Optional[int] = None
    max_whatsapp_messages: Optional[int] = None
    max_workflows: Optional[int] = None
    max_campaigns_per_month: Optional[int] = None
    max_users: Optional[int] = None
    max_concurrent_calls: Optional[int] = None


class LimitOverrides(BaseModel):
    """Per-tenant limit exceptions. Same keys as ``EffectiveLimits``."""

    max_voice_minutes: Optional[int] = Field(default=None, ge=0)
    max_whatsapp_messages: Optional[int] = Field(default=None, ge=0)
    max_workflows: Optional[int] = Field(default=None, ge=0)
    max_campaigns_per_month: Optional[int] = Field(default=None, ge=0)
    max_users: Optional[int] = Field(default=None, ge=0)
    max_concurrent_calls: Optional[int] = Field(default=None, ge=0)


class UsageSnapshot(BaseModel):
    """Consumption of the organization over its current subscription period."""

    voice_minutes: float
    whatsapp_messages: int
    workflows: int
    campaigns_this_period: int
    users: int
    period_start: datetime
    period_end: datetime


class OrganizationUsageSummary(BaseModel):
    """Metered usage shown on the platform-admin organization list."""

    voice_minutes: float
    whatsapp_messages: int


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: Optional[str] = None
    price_amount: float
    currency: str
    billing_interval: str
    trial_days: int
    max_voice_minutes: Optional[int] = None
    max_whatsapp_messages: Optional[int] = None
    max_workflows: Optional[int] = None
    max_campaigns_per_month: Optional[int] = None
    max_users: Optional[int] = None
    max_concurrent_calls: Optional[int] = None
    features: dict
    is_active: bool
    is_public: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class PlanSummary(BaseModel):
    """The plan fields a tenant is allowed to see about its own subscription."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    price_amount: float
    currency: str
    billing_interval: str


class PlanCreateRequest(BaseModel):
    """Body for ``POST /platform-admin/plans``. A NULL limit means unlimited."""

    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None
    price_amount: float = Field(default=0, ge=0)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    billing_interval: BillingInterval = "monthly"
    trial_days: int = Field(default=0, ge=0)
    max_voice_minutes: Optional[int] = Field(default=None, ge=0)
    max_whatsapp_messages: Optional[int] = Field(default=None, ge=0)
    max_workflows: Optional[int] = Field(default=None, ge=0)
    max_campaigns_per_month: Optional[int] = Field(default=None, ge=0)
    max_users: Optional[int] = Field(default=None, ge=0)
    max_concurrent_calls: Optional[int] = Field(default=None, ge=0)
    features: dict = Field(default_factory=dict)
    is_active: bool = True
    is_public: bool = True
    sort_order: int = 0

    @field_validator("code")
    @classmethod
    def code_is_slug(cls, v: str) -> str:
        return _normalize_plan_code(v)

    @field_validator("features")
    @classmethod
    def features_are_complete(cls, v: dict) -> dict:
        return _normalize_features(v)


class PlanUpdateRequest(BaseModel):
    """Body for ``PATCH /platform-admin/plans/{plan_id}``. Partial update.

    Only the fields present in the payload are written, so sending an explicit
    ``null`` on a limit clears it (unlimited) while omitting it leaves it as is.
    """

    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None
    price_amount: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    billing_interval: Optional[BillingInterval] = None
    trial_days: Optional[int] = Field(default=None, ge=0)
    max_voice_minutes: Optional[int] = Field(default=None, ge=0)
    max_whatsapp_messages: Optional[int] = Field(default=None, ge=0)
    max_workflows: Optional[int] = Field(default=None, ge=0)
    max_campaigns_per_month: Optional[int] = Field(default=None, ge=0)
    max_users: Optional[int] = Field(default=None, ge=0)
    max_concurrent_calls: Optional[int] = Field(default=None, ge=0)
    features: Optional[dict] = None
    is_active: Optional[bool] = None
    is_public: Optional[bool] = None
    sort_order: Optional[int] = None

    @field_validator("features")
    @classmethod
    def features_are_complete(cls, v: Optional[dict]) -> Optional[dict]:
        return None if v is None else _normalize_features(v)

    @field_validator("code")
    @classmethod
    def code_is_slug(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_plan_code(v) if v is not None else v


class PlanListResponse(BaseModel):
    plans: List[PlanResponse]


class SeedDefaultPlansResponse(BaseModel):
    """Result of the idempotent default-catalogue seed."""

    created_codes: List[str]
    plans: List[PlanResponse]


# ---------------------------------------------------------------------------
# Platform metrics
# ---------------------------------------------------------------------------


class PlatformMetricsResponse(BaseModel):
    """Headline numbers of the platform-admin dashboard."""

    total_organizations: int
    active_subscriptions: int
    trialing: int
    suspended: int
    past_due: int
    mrr_amount: float
    currency: str
    voice_minutes_this_period: float
    whatsapp_messages_this_period: int


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


class OrganizationProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: Optional[str] = None
    contact_email: Optional[str] = None
    created_at: Optional[datetime] = None


class OrganizationUserSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: Optional[str] = None
    is_superuser: bool


class SubscriptionSummary(BaseModel):
    """The subscription row as the platform admin sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    status: str
    current_period_start: datetime
    current_period_end: datetime
    trial_end_at: Optional[datetime] = None
    cancel_at_period_end: bool
    limit_overrides: dict
    external_reference: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SubscriptionEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    subscription_id: Optional[int] = None
    actor_user_id: Optional[int] = None
    payload: dict
    note: Optional[str] = None
    created_at: Optional[datetime] = None


class SubscriptionEventListResponse(BaseModel):
    events: List[SubscriptionEventResponse]


class OrganizationListItem(BaseModel):
    """One row of the platform-admin tenant table.

    Plan and status fields are null for organizations that predate the
    subscription layer and have not been provisioned yet.
    """

    id: int
    name: Optional[str] = None
    contact_email: Optional[str] = None
    created_at: Optional[datetime] = None
    plan_code: Optional[str] = None
    plan_name: Optional[str] = None
    status: Optional[str] = None
    current_period_end: Optional[datetime] = None
    usage: OrganizationUsageSummary
    limits: EffectiveLimits
    users_count: int


class OrganizationListResponse(BaseModel):
    organizations: List[OrganizationListItem]
    total_count: int
    page: int
    limit: int
    total_pages: int


class OrganizationDetailResponse(BaseModel):
    organization: OrganizationProfile
    subscription: Optional[SubscriptionSummary] = None
    plan: Optional[PlanResponse] = None
    limits: EffectiveLimits
    usage: UsageSnapshot
    users: List[OrganizationUserSummary]
    events: List[SubscriptionEventResponse]


class OrganizationCreateRequest(BaseModel):
    """Body for ``POST /platform-admin/organizations``.

    Provisions the tenant, its owner user and its subscription in one call.
    ``owner_password`` is hashed by the service — it is never stored or logged
    in clear text.
    """

    name: str = Field(..., min_length=1, max_length=255)
    contact_email: EmailStr
    owner_email: EmailStr
    owner_password: str = Field(..., min_length=8)
    plan_code: str = Field(..., min_length=1, max_length=64)
    trial_days: Optional[int] = Field(default=None, ge=0)
    notes: Optional[str] = None


class AssignPlanRequest(BaseModel):
    """Body for ``POST /platform-admin/organizations/{org_id}/subscription``.

    Omitted period bounds are derived from the plan's billing interval.
    """

    plan_code: str = Field(..., min_length=1, max_length=64)
    status: Optional[SubscriptionStatus] = None
    current_period_start: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    limit_overrides: Optional[LimitOverrides] = None
    notes: Optional[str] = None


class SuspendRequest(BaseModel):
    reason: Optional[str] = None


class CancelRequest(BaseModel):
    """``at_period_end`` keeps the tenant running until the period expires."""

    at_period_end: bool = True
    reason: Optional[str] = None


class UpdateLimitsRequest(BaseModel):
    limit_overrides: LimitOverrides


# ---------------------------------------------------------------------------
# Tenant-facing
# ---------------------------------------------------------------------------


class OrganizationSubscriptionResponse(BaseModel):
    """What the current organization is entitled to and what it has used.

    Every subscription field is nullable: organizations created before the
    subscription layer (and OSS deployments) have no subscription row, and the
    enforcement treats that as unlimited rather than blocking them.
    """

    plan: Optional[PlanSummary] = None
    status: Optional[str] = None
    current_period_start: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    trial_end_at: Optional[datetime] = None
    cancel_at_period_end: bool = False
    limits: EffectiveLimits
    usage: UsageSnapshot
    features: dict
