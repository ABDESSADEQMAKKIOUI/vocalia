"""DB operations for the SaaS subscription layer.

Covers the plan catalogue, the per-organization subscription row, the audit
journal of subscription changes, and the usage counters the quota enforcement
compares against the effective limits.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from api.db.base_client import BaseDBClient
from api.db.models import (
    CampaignModel,
    OrganizationModel,
    OrganizationSubscriptionModel,
    SubscriptionEventModel,
    SubscriptionPlanModel,
    WhatsAppConversationModel,
    WorkflowModel,
    WorkflowRunModel,
    organization_users_association,
)
from api.enums import WorkflowRunMode, WorkflowStatus

# Voice runs are every mode except the text-only channels; used to scope the
# voice-minutes counter so WhatsApp and text-chat runs never consume voice quota.
_NON_VOICE_RUN_MODES = (
    WorkflowRunMode.WHATSAPP.value,
    WorkflowRunMode.TEXTCHAT.value,
    WorkflowRunMode.CHAT.value,
)


class SubscriptionClient(BaseDBClient):
    """Client for managing subscription plans, subscriptions and usage counters."""

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------
    async def list_subscription_plans(
        self, *, include_inactive: bool = False
    ) -> list[SubscriptionPlanModel]:
        async with self.async_session() as session:
            query = select(SubscriptionPlanModel).order_by(
                SubscriptionPlanModel.sort_order, SubscriptionPlanModel.id
            )
            if not include_inactive:
                query = query.where(SubscriptionPlanModel.is_active.is_(True))
            result = await session.execute(query)
            return list(result.scalars().all())

    async def get_subscription_plan_by_id(
        self, plan_id: int
    ) -> Optional[SubscriptionPlanModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SubscriptionPlanModel).where(SubscriptionPlanModel.id == plan_id)
            )
            return result.scalars().first()

    async def get_subscription_plan_by_code(
        self, code: str
    ) -> Optional[SubscriptionPlanModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SubscriptionPlanModel).where(SubscriptionPlanModel.code == code)
            )
            return result.scalars().first()

    async def create_subscription_plan(self, **fields) -> SubscriptionPlanModel:
        async with self.async_session() as session:
            plan = SubscriptionPlanModel(**fields)
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
        return plan

    async def update_subscription_plan(
        self, plan_id: int, **fields
    ) -> Optional[SubscriptionPlanModel]:
        """Apply the provided fields to a plan. Unknown keys are ignored."""
        async with self.async_session() as session:
            result = await session.execute(
                select(SubscriptionPlanModel)
                .where(SubscriptionPlanModel.id == plan_id)
                .with_for_update()
            )
            plan = result.scalars().first()
            if not plan:
                return None
            for key, value in fields.items():
                if hasattr(plan, key):
                    setattr(plan, key, value)
            await session.commit()
            await session.refresh(plan)
        return plan

    async def count_subscriptions_for_plan(self, plan_id: int) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                select(func.count(OrganizationSubscriptionModel.id)).where(
                    OrganizationSubscriptionModel.plan_id == plan_id
                )
            )
            return result.scalar() or 0

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------
    async def get_organization_subscription(
        self, organization_id: int
    ) -> Optional[OrganizationSubscriptionModel]:
        """Get the subscription of an organization, with its plan eagerly loaded."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationSubscriptionModel)
                .options(joinedload(OrganizationSubscriptionModel.plan))
                .where(OrganizationSubscriptionModel.organization_id == organization_id)
            )
            return result.scalars().unique().first()

    async def upsert_organization_subscription(
        self, organization_id: int, **fields
    ) -> OrganizationSubscriptionModel:
        """Create the organization subscription or update it in place."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationSubscriptionModel)
                .where(OrganizationSubscriptionModel.organization_id == organization_id)
                .with_for_update()
            )
            subscription = result.scalars().first()
            if subscription is None:
                subscription = OrganizationSubscriptionModel(
                    organization_id=organization_id, **fields
                )
                session.add(subscription)
            else:
                for key, value in fields.items():
                    if hasattr(subscription, key):
                        setattr(subscription, key, value)
            await session.commit()
            await session.refresh(subscription)
        return subscription

    async def list_organization_subscriptions(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[tuple], int]:
        """List organizations with their subscription and plan, paginated.

        Organizations without a subscription are included (LEFT JOIN) so the
        platform admin can see tenants that still need provisioning. Returns
        ``(rows, total_count)`` where each row is
        ``(OrganizationModel, OrganizationSubscriptionModel | None,
        SubscriptionPlanModel | None)``.
        """
        async with self.async_session() as session:
            query = (
                select(
                    OrganizationModel,
                    OrganizationSubscriptionModel,
                    SubscriptionPlanModel,
                )
                .outerjoin(
                    OrganizationSubscriptionModel,
                    OrganizationSubscriptionModel.organization_id
                    == OrganizationModel.id,
                )
                .outerjoin(
                    SubscriptionPlanModel,
                    OrganizationSubscriptionModel.plan_id == SubscriptionPlanModel.id,
                )
            )

            if search:
                pattern = f"%{search}%"
                query = query.where(
                    or_(
                        OrganizationModel.name.ilike(pattern),
                        OrganizationModel.contact_email.ilike(pattern),
                        OrganizationModel.provider_id.ilike(pattern),
                    )
                )
            if status:
                query = query.where(OrganizationSubscriptionModel.status == status)

            count_result = await session.execute(
                select(func.count()).select_from(query.subquery())
            )
            total_count = count_result.scalar() or 0

            result = await session.execute(
                query.order_by(OrganizationModel.id.desc()).limit(limit).offset(offset)
            )
            return list(result.all()), total_count

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    async def create_subscription_event(
        self,
        *,
        organization_id: int,
        event_type: str,
        subscription_id: int | None = None,
        actor_user_id: int | None = None,
        payload: dict | None = None,
        note: str | None = None,
    ) -> SubscriptionEventModel:
        async with self.async_session() as session:
            event = SubscriptionEventModel(
                organization_id=organization_id,
                subscription_id=subscription_id,
                event_type=event_type,
                actor_user_id=actor_user_id,
                payload=payload or {},
                note=note,
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
        return event

    async def list_subscription_events(
        self, organization_id: int, *, limit: int = 50
    ) -> list[SubscriptionEventModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SubscriptionEventModel)
                .where(SubscriptionEventModel.organization_id == organization_id)
                .order_by(SubscriptionEventModel.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Usage counters
    # ------------------------------------------------------------------
    async def count_workflows_for_organization(self, organization_id: int) -> int:
        """Count the non-archived workflows (agents) owned by an organization."""
        async with self.async_session() as session:
            result = await session.execute(
                select(func.count(WorkflowModel.id)).where(
                    WorkflowModel.organization_id == organization_id,
                    WorkflowModel.status != WorkflowStatus.ARCHIVED.value,
                )
            )
            return result.scalar() or 0

    async def count_campaigns_for_organization(
        self, organization_id: int, *, since: datetime
    ) -> int:
        """Count campaigns created by an organization since a point in time."""
        async with self.async_session() as session:
            result = await session.execute(
                select(func.count(CampaignModel.id)).where(
                    CampaignModel.organization_id == organization_id,
                    CampaignModel.created_at >= since,
                )
            )
            return result.scalar() or 0

    async def count_users_for_organization(self, organization_id: int) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(organization_users_association)
                .where(
                    organization_users_association.c.organization_id == organization_id
                )
            )
            return result.scalar() or 0

    async def count_whatsapp_messages_for_organization(
        self, organization_id: int, *, since: datetime, until: datetime
    ) -> int:
        """Count the billable WhatsApp units consumed by an organization.

        Source: ``whatsapp_conversations``. There is no per-message table in
        this schema — individual messages live inside the workflow run
        transcript, and ``whatsapp_processed_messages`` only holds inbound
        wamids for webhook idempotency (no organization_id, and rows are purged
        by the sweeper). A conversation window is therefore the only durable,
        org-scoped unit available, and it also matches how Meta itself bills
        WhatsApp: per conversation, not per message.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(func.count(WhatsAppConversationModel.id)).where(
                    WhatsAppConversationModel.organization_id == organization_id,
                    WhatsAppConversationModel.created_at >= since,
                    WhatsAppConversationModel.created_at < until,
                )
            )
            return result.scalar() or 0

    async def get_voice_seconds_for_organization(
        self, organization_id: int, *, since: datetime, until: datetime
    ) -> float:
        """Sum the voice call seconds consumed by an organization over a window.

        Source: ``workflow_runs.usage_info->>'call_duration_seconds'`` joined to
        ``workflows`` for the org scoping, rather than
        ``organization_usage_cycles.total_duration_seconds``. The usage cycles
        are bound to calendar months, whereas a subscription period can start on
        any day, so aggregating the runs is the only way to measure the exact
        billing window. Text-only run modes are excluded so WhatsApp and
        text-chat sessions never consume voice minutes.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(
                    func.sum(
                        WorkflowRunModel.usage_info["call_duration_seconds"].as_float()
                    )
                )
                .join(WorkflowModel, WorkflowModel.id == WorkflowRunModel.workflow_id)
                .where(
                    WorkflowModel.organization_id == organization_id,
                    WorkflowRunModel.created_at >= since,
                    WorkflowRunModel.created_at < until,
                    WorkflowRunModel.mode.notin_(_NON_VOICE_RUN_MODES),
                )
            )
            return float(result.scalar() or 0)

    # ------------------------------------------------------------------
    # Organizations
    # ------------------------------------------------------------------
    async def update_organization_profile(
        self,
        organization_id: int,
        *,
        name: str | None = None,
        contact_email: str | None = None,
    ) -> Optional[OrganizationModel]:
        """Update the tenant profile fields set by the platform admin."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel)
                .where(OrganizationModel.id == organization_id)
                .with_for_update()
            )
            organization = result.scalars().first()
            if not organization:
                return None
            if name is not None:
                organization.name = name
            if contact_email is not None:
                organization.contact_email = contact_email
            await session.commit()
            await session.refresh(organization)
        return organization

    async def count_organizations(self) -> int:
        async with self.async_session() as session:
            result = await session.execute(select(func.count(OrganizationModel.id)))
            return result.scalar() or 0

    async def get_platform_usage_totals(
        self, *, since: datetime, until: datetime
    ) -> tuple[float, int]:
        """Aggregate voice seconds and WhatsApp units across every organization.

        Uses the same sources as the per-organization counters. Returns
        ``(voice_seconds, whatsapp_messages)``.
        """
        async with self.async_session() as session:
            voice_result = await session.execute(
                select(
                    func.sum(
                        WorkflowRunModel.usage_info["call_duration_seconds"].as_float()
                    )
                ).where(
                    WorkflowRunModel.created_at >= since,
                    WorkflowRunModel.created_at < until,
                    WorkflowRunModel.mode.notin_(_NON_VOICE_RUN_MODES),
                )
            )
            whatsapp_result = await session.execute(
                select(func.count(WhatsAppConversationModel.id)).where(
                    WhatsAppConversationModel.created_at >= since,
                    WhatsAppConversationModel.created_at < until,
                )
            )
            return float(voice_result.scalar() or 0), (whatsapp_result.scalar() or 0)

    async def count_subscriptions_by_status(self) -> dict[str, int]:
        """Return the number of subscriptions per status, for platform metrics."""
        async with self.async_session() as session:
            result = await session.execute(
                select(
                    OrganizationSubscriptionModel.status,
                    func.count(OrganizationSubscriptionModel.id),
                ).group_by(OrganizationSubscriptionModel.status)
            )
            return {status: count for status, count in result.all()}

    async def list_active_subscriptions_with_plans(
        self,
    ) -> list[tuple[OrganizationSubscriptionModel, SubscriptionPlanModel]]:
        """List every non-cancelled subscription with its plan, for MRR maths."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationSubscriptionModel, SubscriptionPlanModel).join(
                    SubscriptionPlanModel,
                    OrganizationSubscriptionModel.plan_id == SubscriptionPlanModel.id,
                )
            )
            return list(result.all())
