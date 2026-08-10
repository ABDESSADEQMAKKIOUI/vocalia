"""Platform-admin endpoints for the SaaS subscription layer.

Every route here is superuser-only: this is the operator console used to manage
the plan catalogue and the tenant organizations, not a tenant-facing surface.
Handlers stay thin — validation and response shaping only; the subscription
rules live in ``api/services/subscription/``.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.schemas.subscription import (
    AssignPlanRequest,
    CancelRequest,
    EffectiveLimits,
    OrganizationCreateRequest,
    OrganizationDetailResponse,
    OrganizationListItem,
    OrganizationListResponse,
    OrganizationProfile,
    OrganizationUsageSummary,
    OrganizationUserSummary,
    PlanCreateRequest,
    PlanListResponse,
    PlanResponse,
    PlanUpdateRequest,
    PlatformMetricsResponse,
    SeedDefaultPlansResponse,
    SubscriptionEventListResponse,
    SubscriptionEventResponse,
    SubscriptionStatus,
    SubscriptionSummary,
    SuspendRequest,
    UpdateLimitsRequest,
    UsageSnapshot,
)
from api.schemas.ai_model_configuration import (
    OrganizationAIModelConfigurationV2,
    compile_ai_model_configuration_v2,
)
from api.services.auth.depends import get_superuser
from api.services.configuration.ai_model_configuration import (
    check_for_masked_keys_in_ai_model_configuration_v2,
    get_organization_ai_model_configuration_v2,
    mask_ai_model_configuration_v2,
    merge_ai_model_configuration_v2_secrets,
    upsert_organization_ai_model_configuration_v2,
)
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.subscription.plans import ensure_default_plans
from api.services.subscription.service import (
    assign_plan,
    cancel,
    get_effective_limits,
    get_platform_metrics,
    get_subscription,
    get_usage_snapshot,
    provision_organization,
    reactivate,
    renew_period,
    suspend,
    update_limits,
)

router = APIRouter(prefix="/platform-admin", tags=["platform-admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ensure_organization(organization_id: int) -> OrganizationModel:
    organization = await db_client.get_organization_by_id(organization_id)
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


async def _ensure_subscription(organization_id: int):
    """Resolve the organization's subscription or 404.

    Organizations without a subscription row are legal (they predate the
    billing layer), but they cannot be suspended, renewed or cancelled.
    """
    await _ensure_organization(organization_id)
    subscription = await get_subscription(organization_id)
    if subscription is None:
        raise HTTPException(
            status_code=404,
            detail="Organization has no subscription. Assign a plan first.",
        )
    return subscription


async def _ensure_plan_by_code(plan_code: str):
    plan = await db_client.get_subscription_plan_by_code(plan_code)
    if not plan:
        raise HTTPException(
            status_code=404, detail=f"Plan '{plan_code}' does not exist"
        )
    if not plan.is_active:
        raise HTTPException(
            status_code=400, detail=f"Plan '{plan_code}' is no longer active"
        )
    return plan


async def _organization_detail(organization_id: int) -> OrganizationDetailResponse:
    """Assemble the full tenant view: profile, subscription, limits and usage."""
    organization = await _ensure_organization(organization_id)
    subscription = await get_subscription(organization_id)
    plan = subscription.plan if subscription else None
    limits = get_effective_limits(subscription, plan)
    usage = await get_usage_snapshot(organization_id)
    users = await db_client.get_organization_users(organization_id)
    events = await db_client.list_subscription_events(organization_id, limit=20)

    return OrganizationDetailResponse(
        organization=OrganizationProfile.model_validate(organization),
        subscription=(
            SubscriptionSummary.model_validate(subscription) if subscription else None
        ),
        plan=PlanResponse.model_validate(plan) if plan else None,
        limits=EffectiveLimits.model_validate(limits),
        usage=UsageSnapshot.model_validate(usage),
        users=[OrganizationUserSummary.model_validate(u) for u in users],
        events=[SubscriptionEventResponse.model_validate(e) for e in events],
    )


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@router.get("/plans", response_model=PlanListResponse)
async def list_plans(
    include_inactive: bool = Query(
        False, description="Include soft-deactivated plans in the catalogue"
    ),
    user: UserModel = Depends(get_superuser),
) -> PlanListResponse:
    """List the plan catalogue."""
    plans = await db_client.list_subscription_plans(include_inactive=include_inactive)
    return PlanListResponse(plans=[PlanResponse.model_validate(p) for p in plans])


@router.post("/plans", response_model=PlanResponse)
async def create_plan(
    request: PlanCreateRequest,
    user: UserModel = Depends(get_superuser),
) -> PlanResponse:
    """Create a plan. Codes are unique across the catalogue."""
    existing = await db_client.get_subscription_plan_by_code(request.code)
    if existing:
        raise HTTPException(
            status_code=409, detail=f"Plan code '{request.code}' is already taken"
        )

    plan = await db_client.create_subscription_plan(**request.model_dump())
    return PlanResponse.model_validate(plan)


@router.post("/plans/seed-defaults", response_model=SeedDefaultPlansResponse)
async def seed_default_plans(
    user: UserModel = Depends(get_superuser),
) -> SeedDefaultPlansResponse:
    """Insert the built-in plan catalogue.

    Idempotent: plans that already exist are left untouched, so only the codes
    created by this call are reported back.
    """
    created_codes = await ensure_default_plans()
    plans = await db_client.list_subscription_plans(include_inactive=True)
    return SeedDefaultPlansResponse(
        created_codes=list(created_codes),
        plans=[PlanResponse.model_validate(p) for p in plans],
    )


@router.patch("/plans/{plan_id}", response_model=PlanResponse)
async def update_plan(
    plan_id: int,
    request: PlanUpdateRequest,
    user: UserModel = Depends(get_superuser),
) -> PlanResponse:
    """Partially update a plan. Absent fields keep their stored value."""
    plan = await db_client.get_subscription_plan_by_id(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    fields = request.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "code" in fields:
        conflict = await db_client.get_subscription_plan_by_code(fields["code"])
        if conflict and conflict.id != plan_id:
            raise HTTPException(
                status_code=409, detail=f"Plan code '{fields['code']}' is already taken"
            )

    updated = await db_client.update_subscription_plan(plan_id, **fields)
    return PlanResponse.model_validate(updated)


@router.delete("/plans/{plan_id}", response_model=PlanResponse)
async def deactivate_plan(
    plan_id: int,
    user: UserModel = Depends(get_superuser),
) -> PlanResponse:
    """Soft-delete a plan by deactivating it.

    Plans are never removed — subscriptions and audit events reference them.
    A plan that still has subscribers cannot be deactivated at all, otherwise
    those tenants would resolve their limits against a hidden plan.
    """
    plan = await db_client.get_subscription_plan_by_id(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    subscribers = await db_client.count_subscriptions_for_plan(plan_id)
    if subscribers:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Plan '{plan.code}' still has {subscribers} subscribed "
                f"organization(s). Move them to another plan first."
            ),
        )

    updated = await db_client.update_subscription_plan(plan_id, is_active=False)
    return PlanResponse.model_validate(updated)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@router.get("/metrics", response_model=PlatformMetricsResponse)
async def get_metrics(
    user: UserModel = Depends(get_superuser),
) -> PlatformMetricsResponse:
    """Platform-wide subscription and usage totals."""
    metrics = await get_platform_metrics()
    return PlatformMetricsResponse.model_validate(metrics)


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


@router.get("/organizations", response_model=OrganizationListResponse)
async def list_organizations(
    search: Optional[str] = Query(
        None, description="Match on organization name or contact email"
    ),
    status: Optional[SubscriptionStatus] = Query(
        None, description="Filter by subscription status"
    ),
    page: int = Query(1, ge=1, description="Page number (starts from 1)"),
    limit: int = Query(25, ge=1, le=100, description="Number of items per page"),
    user: UserModel = Depends(get_superuser),
) -> OrganizationListResponse:
    """Paginated tenant list with plan, quota limits and current usage."""
    offset = (page - 1) * limit
    rows, total_count = await db_client.list_organization_subscriptions(
        search=search,
        status=status,
        limit=limit,
        offset=offset,
    )

    organizations = []
    for organization, subscription, plan in rows:
        limits = get_effective_limits(subscription, plan)
        usage = UsageSnapshot.model_validate(await get_usage_snapshot(organization.id))
        users_count = await db_client.count_users_for_organization(organization.id)
        organizations.append(
            OrganizationListItem(
                id=organization.id,
                name=organization.name,
                contact_email=organization.contact_email,
                created_at=organization.created_at,
                plan_code=plan.code if plan else None,
                plan_name=plan.name if plan else None,
                status=subscription.status if subscription else None,
                current_period_end=(
                    subscription.current_period_end if subscription else None
                ),
                usage=OrganizationUsageSummary(
                    voice_minutes=usage.voice_minutes,
                    whatsapp_messages=usage.whatsapp_messages,
                ),
                limits=EffectiveLimits.model_validate(limits),
                users_count=users_count,
            )
        )

    total_pages = (total_count + limit - 1) // limit
    return OrganizationListResponse(
        organizations=organizations,
        total_count=total_count,
        page=page,
        limit=limit,
        total_pages=total_pages,
    )


@router.post("/organizations", response_model=OrganizationDetailResponse)
async def create_organization(
    request: OrganizationCreateRequest,
    user: UserModel = Depends(get_superuser),
) -> OrganizationDetailResponse:
    """Provision a tenant: organization, owner user and subscription."""
    await _ensure_plan_by_code(request.plan_code)

    existing_owner = await db_client.get_user_by_email(request.owner_email)
    if existing_owner:
        raise HTTPException(
            status_code=409,
            detail=f"A user already exists with email {request.owner_email}",
        )

    try:
        provisioned = await provision_organization(
            name=request.name,
            contact_email=request.contact_email,
            owner_email=request.owner_email,
            owner_password=request.owner_password,
            plan_code=request.plan_code,
            trial_days=request.trial_days,
            notes=request.notes,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return await _organization_detail(provisioned.organization.id)


@router.get("/organizations/{org_id}", response_model=OrganizationDetailResponse)
async def get_organization(
    org_id: int,
    user: UserModel = Depends(get_superuser),
) -> OrganizationDetailResponse:
    """Full tenant view, including the 20 most recent subscription events."""
    return await _organization_detail(org_id)


@router.post(
    "/organizations/{org_id}/subscription", response_model=OrganizationDetailResponse
)
async def set_organization_subscription(
    org_id: int,
    request: AssignPlanRequest,
    user: UserModel = Depends(get_superuser),
) -> OrganizationDetailResponse:
    """Assign a plan to the organization, creating the subscription if needed."""
    await _ensure_organization(org_id)
    await _ensure_plan_by_code(request.plan_code)

    try:
        await assign_plan(
            organization_id=org_id,
            plan_code=request.plan_code,
            status=request.status,
            current_period_start=request.current_period_start,
            current_period_end=request.current_period_end,
            limit_overrides=(
                request.limit_overrides.model_dump(exclude_unset=True)
                if request.limit_overrides is not None
                else None
            ),
            notes=request.notes,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return await _organization_detail(org_id)


@router.post(
    "/organizations/{org_id}/suspend", response_model=OrganizationDetailResponse
)
async def suspend_organization(
    org_id: int,
    request: SuspendRequest,
    user: UserModel = Depends(get_superuser),
) -> OrganizationDetailResponse:
    """Suspend the tenant. Suspended organizations are denied at run start."""
    await _ensure_subscription(org_id)

    try:
        await suspend(
            organization_id=org_id,
            reason=request.reason,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return await _organization_detail(org_id)


@router.post(
    "/organizations/{org_id}/reactivate", response_model=OrganizationDetailResponse
)
async def reactivate_organization(
    org_id: int,
    user: UserModel = Depends(get_superuser),
) -> OrganizationDetailResponse:
    """Lift a suspension and put the subscription back into service."""
    await _ensure_subscription(org_id)

    try:
        await reactivate(organization_id=org_id, actor_user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return await _organization_detail(org_id)


@router.post(
    "/organizations/{org_id}/cancel", response_model=OrganizationDetailResponse
)
async def cancel_organization(
    org_id: int,
    request: CancelRequest,
    user: UserModel = Depends(get_superuser),
) -> OrganizationDetailResponse:
    """Cancel the subscription, either at period end or immediately."""
    await _ensure_subscription(org_id)

    try:
        await cancel(
            organization_id=org_id,
            at_period_end=request.at_period_end,
            reason=request.reason,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return await _organization_detail(org_id)


@router.post("/organizations/{org_id}/renew", response_model=OrganizationDetailResponse)
async def renew_organization(
    org_id: int,
    user: UserModel = Depends(get_superuser),
) -> OrganizationDetailResponse:
    """Advance the billing period by one interval, resetting metered usage."""
    await _ensure_subscription(org_id)

    try:
        await renew_period(organization_id=org_id, actor_user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return await _organization_detail(org_id)


@router.patch(
    "/organizations/{org_id}/limits", response_model=OrganizationDetailResponse
)
async def update_organization_limits(
    org_id: int,
    request: UpdateLimitsRequest,
    user: UserModel = Depends(get_superuser),
) -> OrganizationDetailResponse:
    """Replace the tenant's limit overrides. Omitted keys fall back to the plan."""
    await _ensure_subscription(org_id)

    try:
        await update_limits(
            organization_id=org_id,
            limit_overrides=request.limit_overrides.model_dump(exclude_unset=True),
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return await _organization_detail(org_id)


@router.get("/organizations/{org_id}/usage", response_model=UsageSnapshot)
async def get_organization_usage(
    org_id: int,
    user: UserModel = Depends(get_superuser),
) -> UsageSnapshot:
    """Metered consumption of the tenant over its current period."""
    await _ensure_organization(org_id)
    usage = await get_usage_snapshot(org_id)
    return UsageSnapshot.model_validate(usage)


@router.get(
    "/organizations/{org_id}/events", response_model=SubscriptionEventListResponse
)
async def get_organization_events(
    org_id: int,
    limit: int = Query(50, ge=1, le=200, description="Number of events to return"),
    user: UserModel = Depends(get_superuser),
) -> SubscriptionEventListResponse:
    """Audit trail of everything that happened to the tenant's subscription."""
    await _ensure_organization(org_id)
    events = await db_client.list_subscription_events(org_id, limit=limit)
    return SubscriptionEventListResponse(
        events=[SubscriptionEventResponse.model_validate(e) for e in events]
    )


# ---------------------------------------------------------------------------
# Tenant model configuration
# ---------------------------------------------------------------------------
#
# Model choice belongs to the platform admin, but the organization routes act on
# the CALLER's own organization — which is the admin's, not the tenant's. On a
# deployment without Stack Auth there is no impersonation to borrow a tenant's
# session with (AUTH_PROVIDER=local), so without these two routes locking the
# organization ones simply means nobody can configure a client's models at all.


@router.get("/organizations/{org_id}/model-configuration")
async def get_organization_model_configuration(
    org_id: int,
    user: UserModel = Depends(get_superuser),
):
    """The tenant's model configuration, with its provider keys masked.

    Masked even for a platform admin: the response reaches a browser, and a key
    that never leaves the server cannot leak from one.
    """
    await _ensure_organization(org_id)
    configuration = await get_organization_ai_model_configuration_v2(org_id)
    return {
        "organization_id": org_id,
        "configuration": (
            mask_ai_model_configuration_v2(configuration) if configuration else None
        ),
    }


@router.put("/organizations/{org_id}/model-configuration")
async def set_organization_model_configuration(
    org_id: int,
    request: OrganizationAIModelConfigurationV2,
    user: UserModel = Depends(get_superuser),
):
    """Set the tenant's model configuration.

    Same validation as the organization route it mirrors — merge the masked
    secrets back from what is stored, refuse a payload that still carries a mask
    (that would persist the mask as if it were the key), compile, and validate
    against the providers before writing.
    """
    await _ensure_organization(org_id)
    existing = await get_organization_ai_model_configuration_v2(org_id)
    configuration = merge_ai_model_configuration_v2_secrets(request, existing)
    try:
        check_for_masked_keys_in_ai_model_configuration_v2(configuration)
        effective = compile_ai_model_configuration_v2(configuration)
        await UserConfigurationValidator().validate(
            effective,
            organization_id=org_id,
            created_by=user.provider_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0])

    await upsert_organization_ai_model_configuration_v2(org_id, configuration)
    logger.info(
        f"Platform admin {user.id} set the model configuration of organization {org_id}"
    )
    return {
        "organization_id": org_id,
        "configuration": mask_ai_model_configuration_v2(configuration),
    }
