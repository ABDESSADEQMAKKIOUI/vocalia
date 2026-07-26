"""add subscription plans, organization subscriptions and events

Revision ID: a1c7f3e9d240
Revises: c9f2e3a4b5d6
Create Date: 2026-07-25 10:00:00.000000

Introduces the SaaS billing foundation: the catalogue of commercial plans,
the single subscription row each tenant organization carries, and the audit
journal of subscription changes. Also adds the tenant profile columns
(name, contact_email) the platform admin fills in when provisioning an org.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1c7f3e9d240"
down_revision: Union[str, None] = "c9f2e3a4b5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subscription_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "price_amount", sa.Float(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default=sa.text("'EUR'"),
        ),
        sa.Column(
            "billing_interval",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'monthly'"),
        ),
        sa.Column(
            "trial_days", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("max_voice_minutes", sa.Integer(), nullable=True),
        sa.Column("max_whatsapp_messages", sa.Integer(), nullable=True),
        sa.Column("max_workflows", sa.Integer(), nullable=True),
        sa.Column("max_campaigns_per_month", sa.Integer(), nullable=True),
        sa.Column("max_users", sa.Integer(), nullable=True),
        sa.Column("max_concurrent_calls", sa.Integer(), nullable=True),
        sa.Column(
            "features",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_subscription_plans_id"), "subscription_plans", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_subscription_plans_code"), "subscription_plans", ["code"], unique=True
    )

    op.create_table(
        "organization_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'trialing'"),
        ),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trial_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "limit_overrides",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("external_reference", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["subscription_plans.id"]),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_organization_subscriptions_id"),
        "organization_subscriptions",
        ["id"],
        unique=False,
    )
    op.create_index(
        "uq_organization_subscriptions_org",
        "organization_subscriptions",
        ["organization_id"],
        unique=True,
    )

    op.create_table(
        "subscription_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["organization_subscriptions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_subscription_events_id"), "subscription_events", ["id"], unique=False
    )
    op.create_index(
        "ix_subscription_events_organization_id",
        "subscription_events",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_subscription_events_created_at",
        "subscription_events",
        ["created_at"],
        unique=False,
    )

    op.add_column(
        "organizations", sa.Column("name", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "organizations",
        sa.Column("contact_email", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "contact_email")
    op.drop_column("organizations", "name")

    op.drop_index("ix_subscription_events_created_at", table_name="subscription_events")
    op.drop_index(
        "ix_subscription_events_organization_id", table_name="subscription_events"
    )
    op.drop_index(op.f("ix_subscription_events_id"), table_name="subscription_events")
    op.drop_table("subscription_events")

    op.drop_index(
        "uq_organization_subscriptions_org", table_name="organization_subscriptions"
    )
    op.drop_index(
        op.f("ix_organization_subscriptions_id"),
        table_name="organization_subscriptions",
    )
    op.drop_table("organization_subscriptions")

    op.drop_index(op.f("ix_subscription_plans_code"), table_name="subscription_plans")
    op.drop_index(op.f("ix_subscription_plans_id"), table_name="subscription_plans")
    op.drop_table("subscription_plans")
