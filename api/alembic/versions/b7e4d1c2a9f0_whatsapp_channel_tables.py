"""whatsapp channel tables

Revision ID: b7e4d1c2a9f0
Revises: 00b0201ad918
Create Date: 2026-07-16 12:00:00.000000

Adds the messaging-channel storage for the WhatsApp (Meta Cloud API)
channel: org configurations (credentials encrypted at the app layer),
business phone numbers with inbound workflow routing, conversation-window
to workflow-run mapping, webhook idempotency records, the local mirror of
Meta message templates, and the do-not-message suppression list.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e4d1c2a9f0"
down_revision: Union[str, None] = "00b0201ad918"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "messaging_configurations",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("credentials", sa.JSON(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "name", name="uq_messaging_configurations_org_name"
        ),
    )
    op.create_index(
        "ix_messaging_configurations_org", "messaging_configurations", ["organization_id"]
    )

    op.create_table(
        "messaging_addresses",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "messaging_configuration_id",
            sa.Integer(),
            sa.ForeignKey("messaging_configurations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("address", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=True),
        sa.Column(
            "inbound_workflow_id",
            sa.Integer(),
            sa.ForeignKey("workflows.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "extra_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("external_id", name="uq_messaging_addresses_external_id"),
    )
    op.create_index("ix_messaging_addresses_org", "messaging_addresses", ["organization_id"])
    op.create_index(
        "ix_messaging_addresses_config",
        "messaging_addresses",
        ["messaging_configuration_id"],
    )

    op.create_table(
        "whatsapp_conversations",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "messaging_address_id",
            sa.Integer(),
            sa.ForeignKey("messaging_addresses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phone_number_id", sa.String(64), nullable=False),
        sa.Column("wa_id", sa.String(32), nullable=False),
        sa.Column("profile_name", sa.String(255), nullable=True),
        sa.Column(
            "workflow_run_id",
            sa.Integer(),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "state", sa.String(16), nullable=False, server_default=sa.text("'open'")
        ),
        sa.Column("service_window_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_whatsapp_conversations_open",
        "whatsapp_conversations",
        ["phone_number_id", "wa_id"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )
    op.create_index(
        "ix_whatsapp_conversations_run", "whatsapp_conversations", ["workflow_run_id"]
    )
    op.create_index(
        "ix_whatsapp_conversations_state_updated",
        "whatsapp_conversations",
        ["state", "updated_at"],
    )
    op.create_index(
        "ix_whatsapp_conversations_org", "whatsapp_conversations", ["organization_id"]
    )

    op.create_table(
        "whatsapp_processed_messages",
        sa.Column("wamid", sa.String(128), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_whatsapp_processed_messages_at",
        "whatsapp_processed_messages",
        ["received_at"],
    )

    op.create_table(
        "whatsapp_templates",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "messaging_configuration_id",
            sa.Integer(),
            sa.ForeignKey("messaging_configurations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("meta_template_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column(
            "parameter_format",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'positional'"),
        ),
        sa.Column(
            "components",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default=sa.text("'DRAFT'")
        ),
        sa.Column("quality_score", sa.String(16), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("category_pending_change", sa.String(32), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "messaging_configuration_id",
            "name",
            "language",
            name="uq_whatsapp_templates_config_name_lang",
        ),
    )
    op.create_index("ix_whatsapp_templates_org", "whatsapp_templates", ["organization_id"])
    op.create_index(
        "ix_whatsapp_templates_meta_id", "whatsapp_templates", ["meta_template_id"]
    )

    op.create_table(
        "messaging_suppressions",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("address", sa.String(64), nullable=False),
        sa.Column(
            "scope",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'marketing'"),
        ),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id",
            "address",
            "scope",
            name="uq_messaging_suppressions_org_address_scope",
        ),
    )
    op.create_index(
        "ix_messaging_suppressions_org_address",
        "messaging_suppressions",
        ["organization_id", "address"],
    )


    op.add_column(
        "campaigns",
        sa.Column(
            "channel",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'voice'"),
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "messaging_configuration_id",
            sa.Integer(),
            sa.ForeignKey(
                "messaging_configurations.id",
                ondelete="SET NULL",
                name="fk_campaigns_messaging_configuration_id",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "whatsapp_template_id",
            sa.Integer(),
            sa.ForeignKey(
                "whatsapp_templates.id",
                ondelete="SET NULL",
                name="fk_campaigns_whatsapp_template_id",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "whatsapp_template_id")
    op.drop_column("campaigns", "messaging_configuration_id")
    op.drop_column("campaigns", "channel")
    op.drop_table("messaging_suppressions")
    op.drop_table("whatsapp_templates")
    op.drop_table("whatsapp_processed_messages")
    op.drop_table("whatsapp_conversations")
    op.drop_table("messaging_addresses")
    op.drop_table("messaging_configurations")
