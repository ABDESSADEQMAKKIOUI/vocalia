import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.enums import WorkflowRunMode
from api.routes import platform_admin
from api.services.auth.depends import get_superuser
from api.services.subscription import enforcement, plans
from api.services.subscription.plans import FEATURE_KEYS
from api.services.subscription.service import get_effective_limits

ORGANIZATION_ID = 42


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plan(**overrides):
    plan = {
        "id": 3,
        "code": "starter",
        "name": "Starter",
        "description": None,
        "price_amount": 149.0,
        "currency": "EUR",
        "billing_interval": "monthly",
        "trial_days": 0,
        "max_voice_minutes": 500,
        "max_whatsapp_messages": 2000,
        "max_workflows": 10,
        "max_campaigns_per_month": 5,
        "max_users": 10,
        "max_concurrent_calls": 5,
        "features": {key: True for key in FEATURE_KEYS},
        "is_active": True,
        "is_public": True,
        "sort_order": 1,
        "created_at": _now(),
        "updated_at": _now(),
    }
    plan.update(overrides)
    return SimpleNamespace(**plan)


def _subscription(
    *,
    status: str = "active",
    plan=None,
    limit_overrides: dict | None = None,
    current_period_end: datetime | None = None,
):
    now = _now()
    return SimpleNamespace(
        id=11,
        organization_id=ORGANIZATION_ID,
        plan_id=3,
        plan=_plan() if plan is None else plan,
        status=status,
        current_period_start=now - timedelta(days=5),
        current_period_end=(
            now + timedelta(days=25) if current_period_end is None else current_period_end
        ),
        trial_end_at=None,
        cancel_at_period_end=False,
        limit_overrides={} if limit_overrides is None else limit_overrides,
        external_reference=None,
        notes=None,
        created_at=now,
        updated_at=now,
    )


def _usage_snapshot():
    now = _now()
    return {
        "voice_minutes": 12.5,
        "whatsapp_messages": 40,
        "workflows": 2,
        "campaigns_this_period": 1,
        "users": 3,
        "period_start": (now - timedelta(days=5)).isoformat(),
        "period_end": (now + timedelta(days=25)).isoformat(),
    }


def _patch_enforcement(
    monkeypatch,
    *,
    subscription,
    voice_seconds: float = 0.0,
    whatsapp_messages: int = 0,
) -> AsyncMock:
    """Wire the enforcement module onto in-memory data, return its event mock."""
    record_event = AsyncMock()
    monkeypatch.setattr(
        enforcement.db_client,
        "get_organization_subscription",
        AsyncMock(return_value=subscription),
    )
    monkeypatch.setattr(
        enforcement.db_client,
        "get_voice_seconds_for_organization",
        AsyncMock(return_value=voice_seconds),
    )
    monkeypatch.setattr(
        enforcement.db_client,
        "count_whatsapp_messages_for_organization",
        AsyncMock(return_value=whatsapp_messages),
    )
    monkeypatch.setattr(enforcement, "record_event", record_event)
    return record_event


def _assert_blocked(record_event: AsyncMock, error_code: str) -> None:
    record_event.assert_awaited_once()
    assert record_event.await_args.args == (ORGANIZATION_ID, "quota_blocked")
    assert record_event.await_args.kwargs["payload"]["error_code"] == error_code


# ---------------------------------------------------------------------------
# Effective limits
# ---------------------------------------------------------------------------


def test_effective_limits_without_overrides_are_the_plan_ceilings():
    plan = _plan()
    limits = get_effective_limits(_subscription(plan=plan), plan)

    assert limits == {
        "max_voice_minutes": 500,
        "max_whatsapp_messages": 2000,
        "max_workflows": 10,
        "max_campaigns_per_month": 5,
        "max_users": 10,
        "max_concurrent_calls": 5,
    }


def test_effective_limits_override_wins_over_the_plan():
    plan = _plan()
    subscription = _subscription(
        plan=plan, limit_overrides={"max_voice_minutes": 5000, "max_users": 1}
    )

    limits = get_effective_limits(subscription, plan)

    assert limits["max_voice_minutes"] == 5000
    assert limits["max_users"] == 1


def test_effective_limits_explicit_none_override_means_unlimited():
    """A key mapped to None is an override, not an absent key: unlimited."""
    plan = _plan()
    subscription = _subscription(plan=plan, limit_overrides={"max_voice_minutes": None})

    limits = get_effective_limits(subscription, plan)

    assert limits["max_voice_minutes"] is None
    assert limits["max_whatsapp_messages"] == plan.max_whatsapp_messages


def test_effective_limits_absent_override_key_falls_back_to_the_plan():
    plan = _plan()
    subscription = _subscription(plan=plan, limit_overrides={"max_workflows": 99})

    limits = get_effective_limits(subscription, plan)

    assert limits["max_workflows"] == 99
    assert limits["max_voice_minutes"] == plan.max_voice_minutes
    assert limits["max_campaigns_per_month"] == plan.max_campaigns_per_month
    assert limits["max_concurrent_calls"] == plan.max_concurrent_calls


def test_effective_limits_are_unlimited_without_a_plan():
    limits = get_effective_limits(None, None)

    assert all(value is None for value in limits.values())


# ---------------------------------------------------------------------------
# check_run_allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_run_allowed_without_subscription_allows(monkeypatch):
    """Organizations predating the billing layer must never be blocked."""
    record_event = _patch_enforcement(monkeypatch, subscription=None)

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is True
    assert check.error_code == ""
    record_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_run_allowed_denies_suspended_subscription(monkeypatch):
    record_event = _patch_enforcement(
        monkeypatch, subscription=_subscription(status="suspended")
    )

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is False
    assert check.error_code == "subscription_suspended"
    assert check.error_message
    _assert_blocked(record_event, "subscription_suspended")


@pytest.mark.asyncio
async def test_check_run_allowed_denies_cancelled_subscription(monkeypatch):
    record_event = _patch_enforcement(
        monkeypatch, subscription=_subscription(status="cancelled")
    )

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is False
    assert check.error_code == "subscription_cancelled"
    _assert_blocked(record_event, "subscription_cancelled")


@pytest.mark.asyncio
async def test_check_run_allowed_denies_elapsed_period(monkeypatch):
    """An expired period stays expired: the check never auto-renews it."""
    subscription = _subscription(current_period_end=_now() - timedelta(minutes=1))
    record_event = _patch_enforcement(monkeypatch, subscription=subscription)

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is False
    assert check.error_code == "subscription_expired"
    _assert_blocked(record_event, "subscription_expired")


@pytest.mark.asyncio
async def test_check_run_allowed_denies_when_voice_quota_is_reached(monkeypatch):
    subscription = _subscription(plan=_plan(max_voice_minutes=60))
    record_event = _patch_enforcement(
        monkeypatch, subscription=subscription, voice_seconds=3600
    )

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is False
    assert check.error_code == "voice_quota_exceeded"
    enforcement.db_client.get_voice_seconds_for_organization.assert_awaited_once_with(
        ORGANIZATION_ID,
        since=subscription.current_period_start,
        until=subscription.current_period_end,
    )
    _assert_blocked(record_event, "voice_quota_exceeded")


@pytest.mark.asyncio
async def test_check_run_allowed_spares_text_runs_from_the_voice_quota(monkeypatch):
    """A spent voice quota must not silence WhatsApp and text-chat runs.

    Both channels go through authorize_workflow_run_start, so gating every run
    on max_voice_minutes would take a trial tenant's WhatsApp conversations
    down the moment its voice minutes ran out.
    """
    subscription = _subscription(plan=_plan(max_voice_minutes=60))
    record_event = _patch_enforcement(
        monkeypatch, subscription=subscription, voice_seconds=3600
    )

    for run_mode in sorted(enforcement.NON_VOICE_RUN_MODES):
        check = await enforcement.check_run_allowed(ORGANIZATION_ID, run_mode)
        assert check.allowed is True, run_mode

    enforcement.db_client.get_voice_seconds_for_organization.assert_not_awaited()
    record_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_run_allowed_denies_a_text_run_without_the_whatsapp_feature(
    monkeypatch,
):
    """Text runs are gated on the whatsapp feature, not on the voice one."""
    subscription = _subscription(
        plan=_plan(features={"voice": True, "whatsapp": False})
    )
    record_event = _patch_enforcement(monkeypatch, subscription=subscription)

    check = await enforcement.check_run_allowed(
        ORGANIZATION_ID, WorkflowRunMode.WHATSAPP.value
    )

    assert check.allowed is False
    assert check.error_code == "feature_not_in_plan"
    _assert_blocked(record_event, "feature_not_in_plan")


@pytest.mark.asyncio
async def test_check_run_allowed_runs_a_voice_only_plan_text_channel_check(monkeypatch):
    """A voice-disabled plan still serves WhatsApp: no voice feature required."""
    subscription = _subscription(
        plan=_plan(features={"voice": False, "whatsapp": True})
    )
    _patch_enforcement(monkeypatch, subscription=subscription)

    text = await enforcement.check_run_allowed(
        ORGANIZATION_ID, WorkflowRunMode.WHATSAPP.value
    )
    assert text.allowed is True

    voice = await enforcement.check_run_allowed(
        ORGANIZATION_ID, WorkflowRunMode.WEBRTC.value
    )
    assert voice.allowed is False
    assert voice.error_code == "feature_not_in_plan"


@pytest.mark.asyncio
async def test_check_run_allowed_allows_below_voice_quota(monkeypatch):
    record_event = _patch_enforcement(
        monkeypatch,
        subscription=_subscription(plan=_plan(max_voice_minutes=60)),
        voice_seconds=1800,
    )

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is True
    record_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_run_allowed_allows_when_voice_limit_is_unlimited(monkeypatch):
    """A NULL limit is unlimited, and must not even cost a usage query."""
    _patch_enforcement(
        monkeypatch,
        subscription=_subscription(plan=_plan(max_voice_minutes=None)),
        voice_seconds=999999,
    )

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is True
    enforcement.db_client.get_voice_seconds_for_organization.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_run_allowed_denies_when_voice_is_not_in_the_plan(monkeypatch):
    plan = _plan(features={**{key: True for key in FEATURE_KEYS}, "voice": False})
    record_event = _patch_enforcement(monkeypatch, subscription=_subscription(plan=plan))

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is False
    assert check.error_code == "feature_not_in_plan"
    _assert_blocked(record_event, "feature_not_in_plan")


@pytest.mark.asyncio
async def test_check_run_allowed_allows_when_enforcement_is_disabled(monkeypatch):
    monkeypatch.setattr(enforcement, "SUBSCRIPTION_ENFORCEMENT_ENABLED", False)
    monkeypatch.setattr(
        enforcement.db_client,
        "get_organization_subscription",
        AsyncMock(side_effect=AssertionError("disabled enforcement must not read the DB")),
    )

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is True


@pytest.mark.asyncio
async def test_check_run_allowed_fails_open_when_the_database_is_unavailable(
    monkeypatch,
):
    """A billing-layer incident must never cut a customer's voice production."""
    monkeypatch.setattr(
        enforcement.db_client,
        "get_organization_subscription",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is True
    assert check.error_code == ""


@pytest.mark.asyncio
async def test_check_run_allowed_fails_open_when_usage_lookup_raises(monkeypatch):
    _patch_enforcement(monkeypatch, subscription=_subscription())
    monkeypatch.setattr(
        enforcement.db_client,
        "get_voice_seconds_for_organization",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    check = await enforcement.check_run_allowed(ORGANIZATION_ID)

    assert check.allowed is True


# ---------------------------------------------------------------------------
# check_feature_allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_feature_allowed_denies_a_feature_absent_from_the_plan(monkeypatch):
    plan = _plan(features={**{key: True for key in FEATURE_KEYS}, "campaigns": False})
    record_event = _patch_enforcement(monkeypatch, subscription=_subscription(plan=plan))

    check = await enforcement.check_feature_allowed(ORGANIZATION_ID, "campaigns")

    assert check.allowed is False
    assert check.error_code == "feature_not_in_plan"
    assert enforcement.FEATURE_LABELS["campaigns"] in check.error_message
    _assert_blocked(record_event, "feature_not_in_plan")


@pytest.mark.asyncio
async def test_check_feature_allowed_allows_a_feature_in_the_plan(monkeypatch):
    record_event = _patch_enforcement(monkeypatch, subscription=_subscription())

    check = await enforcement.check_feature_allowed(ORGANIZATION_ID, "campaigns")

    assert check.allowed is True
    record_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_feature_allowed_denies_a_suspended_organization(monkeypatch):
    record_event = _patch_enforcement(
        monkeypatch, subscription=_subscription(status="suspended")
    )

    check = await enforcement.check_feature_allowed(ORGANIZATION_ID, "whatsapp")

    assert check.allowed is False
    assert check.error_code == "subscription_suspended"
    _assert_blocked(record_event, "subscription_suspended")


@pytest.mark.asyncio
async def test_check_feature_allowed_without_subscription_allows(monkeypatch):
    _patch_enforcement(monkeypatch, subscription=None)

    check = await enforcement.check_feature_allowed(ORGANIZATION_ID, "api_access")

    assert check.allowed is True


# ---------------------------------------------------------------------------
# check_whatsapp_message_allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_whatsapp_message_denies_when_quota_is_reached(monkeypatch):
    subscription = _subscription(plan=_plan(max_whatsapp_messages=200))
    record_event = _patch_enforcement(
        monkeypatch, subscription=subscription, whatsapp_messages=200
    )

    check = await enforcement.check_whatsapp_message_allowed(ORGANIZATION_ID)

    assert check.allowed is False
    assert check.error_code == "whatsapp_quota_exceeded"
    enforcement.db_client.count_whatsapp_messages_for_organization.assert_awaited_once_with(
        ORGANIZATION_ID,
        since=subscription.current_period_start,
        until=subscription.current_period_end,
    )
    _assert_blocked(record_event, "whatsapp_quota_exceeded")


@pytest.mark.asyncio
async def test_check_whatsapp_message_denies_a_batch_over_the_remaining_quota(
    monkeypatch,
):
    record_event = _patch_enforcement(
        monkeypatch,
        subscription=_subscription(plan=_plan(max_whatsapp_messages=200)),
        whatsapp_messages=198,
    )

    check = await enforcement.check_whatsapp_message_allowed(ORGANIZATION_ID, count=5)

    assert check.allowed is False
    assert check.error_code == "whatsapp_quota_exceeded"
    _assert_blocked(record_event, "whatsapp_quota_exceeded")


@pytest.mark.asyncio
async def test_check_whatsapp_message_allows_a_batch_within_the_quota(monkeypatch):
    record_event = _patch_enforcement(
        monkeypatch,
        subscription=_subscription(plan=_plan(max_whatsapp_messages=200)),
        whatsapp_messages=190,
    )

    check = await enforcement.check_whatsapp_message_allowed(ORGANIZATION_ID, count=10)

    assert check.allowed is True
    record_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_whatsapp_message_denies_when_whatsapp_is_not_in_the_plan(
    monkeypatch,
):
    plan = _plan(features={**{key: True for key in FEATURE_KEYS}, "whatsapp": False})
    record_event = _patch_enforcement(monkeypatch, subscription=_subscription(plan=plan))

    check = await enforcement.check_whatsapp_message_allowed(ORGANIZATION_ID)

    assert check.allowed is False
    assert check.error_code == "feature_not_in_plan"
    _assert_blocked(record_event, "feature_not_in_plan")


# ---------------------------------------------------------------------------
# Default plan catalogue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_default_plans_is_idempotent(monkeypatch):
    stored: list[SimpleNamespace] = []

    async def _list_subscription_plans(include_inactive: bool = False):
        return list(stored)

    async def _create_subscription_plan(**fields):
        plan = SimpleNamespace(**fields)
        stored.append(plan)
        return plan

    monkeypatch.setattr(
        plans.db_client, "list_subscription_plans", _list_subscription_plans
    )
    monkeypatch.setattr(
        plans.db_client, "create_subscription_plan", _create_subscription_plan
    )

    first_run = await plans.ensure_default_plans()
    second_run = await plans.ensure_default_plans()

    assert first_run == [plan["code"] for plan in plans.DEFAULT_PLANS]
    assert second_run == []
    assert len(stored) == len(plans.DEFAULT_PLANS)
    assert len({plan.code for plan in stored}) == len(plans.DEFAULT_PLANS)


@pytest.mark.asyncio
async def test_ensure_default_plans_only_creates_the_missing_codes(monkeypatch):
    stored = [SimpleNamespace(code=plans.DEFAULT_PLANS[0]["code"], is_active=False)]
    created: list[dict] = []

    async def _list_subscription_plans(include_inactive: bool = False):
        return list(stored)

    async def _create_subscription_plan(**fields):
        created.append(fields)
        plan = SimpleNamespace(**fields)
        stored.append(plan)
        return plan

    monkeypatch.setattr(
        plans.db_client, "list_subscription_plans", _list_subscription_plans
    )
    monkeypatch.setattr(
        plans.db_client, "create_subscription_plan", _create_subscription_plan
    )

    result = await plans.ensure_default_plans()

    assert plans.DEFAULT_PLANS[0]["code"] not in result
    assert result == [plan["code"] for plan in plans.DEFAULT_PLANS[1:]]
    # The seed must hand over copies: editing a stored plan can't mutate the seed.
    assert created[0]["features"] is not plans.DEFAULT_PLANS[1]["features"]


# ---------------------------------------------------------------------------
# Platform-admin routes
# ---------------------------------------------------------------------------


def _make_platform_admin_app() -> FastAPI:
    app = FastAPI()
    app.include_router(platform_admin.router)
    return app


def _platform_admin_endpoints() -> list[tuple[str, str]]:
    """Every (method, concrete path) pair exposed by the platform-admin router."""
    endpoints = []
    for route in platform_admin.router.routes:
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            endpoints.append((method, path))
    return endpoints


def _actor(*, is_superuser: bool):
    return SimpleNamespace(
        id=9,
        email="ops@dograh.test",
        is_superuser=is_superuser,
        selected_organization_id=1,
    )


@pytest.mark.parametrize("method,path", _platform_admin_endpoints())
def test_platform_admin_routes_reject_a_non_superuser(method: str, path: str):
    """The operator console is superuser-only, on every verb and every path."""
    client = TestClient(_make_platform_admin_app())

    with (
        patch(
            "api.services.auth.depends.get_user",
            new=AsyncMock(return_value=_actor(is_superuser=False)),
        ),
        patch("api.routes.platform_admin.db_client") as mock_db,
    ):
        response = client.request(method, path)

    assert response.status_code == 403
    assert mock_db.mock_calls == []


def test_platform_admin_routes_all_declare_the_superuser_dependency():
    for route in platform_admin.router.routes:
        dependencies = [dependency.call for dependency in route.dependant.dependencies]
        assert get_superuser in dependencies, f"{route.path} is not superuser-gated"


def test_platform_admin_organization_detail_exposes_limits_and_usage():
    """The detail view merges plan ceilings, tenant overrides and metered usage."""
    client = TestClient(_make_platform_admin_app())

    plan = _plan()
    subscription = _subscription(plan=plan, limit_overrides={"max_voice_minutes": 750})
    usage = _usage_snapshot()
    organization = SimpleNamespace(
        id=ORGANIZATION_ID,
        name="Acme",
        contact_email="ops@acme.test",
        created_at=_now(),
    )

    with (
        patch(
            "api.services.auth.depends.get_user",
            new=AsyncMock(return_value=_actor(is_superuser=True)),
        ),
        patch("api.routes.platform_admin.db_client") as mock_db,
        patch(
            "api.routes.platform_admin.get_subscription",
            new=AsyncMock(return_value=subscription),
        ),
        patch(
            "api.routes.platform_admin.get_usage_snapshot",
            new=AsyncMock(return_value=usage),
        ),
    ):
        mock_db.get_organization_by_id = AsyncMock(return_value=organization)
        mock_db.get_organization_users = AsyncMock(
            return_value=[
                SimpleNamespace(id=7, email="owner@acme.test", is_superuser=False)
            ]
        )
        mock_db.list_subscription_events = AsyncMock(return_value=[])

        response = client.get(f"/platform-admin/organizations/{ORGANIZATION_ID}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["plan"]["code"] == plan.code
    assert payload["subscription"]["status"] == subscription.status
    assert payload["limits"]["max_voice_minutes"] == 750
    assert payload["limits"]["max_whatsapp_messages"] == plan.max_whatsapp_messages
    assert payload["usage"]["voice_minutes"] == usage["voice_minutes"]
    assert payload["users"][0]["email"] == "owner@acme.test"


def test_platform_admin_lists_the_plan_catalogue_for_a_superuser():
    client = TestClient(_make_platform_admin_app())

    with (
        patch(
            "api.services.auth.depends.get_user",
            new=AsyncMock(return_value=_actor(is_superuser=True)),
        ),
        patch("api.routes.platform_admin.db_client") as mock_db,
    ):
        mock_db.list_subscription_plans = AsyncMock(return_value=[_plan()])
        response = client.get("/platform-admin/plans")

    assert response.status_code == 200
    assert [plan["code"] for plan in response.json()["plans"]] == ["starter"]
