"""whatsapp conversation agent_paused (human takeover)

Revision ID: c9f2e3a4b5d6
Revises: b7e4d1c2a9f0
Create Date: 2026-07-18 21:00:00.000000

Adds the human-takeover flag to whatsapp_conversations: when set, inbound
messages are recorded but the agent does not reply — a human answers from
the inbox UI.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9f2e3a4b5d6"
down_revision: Union[str, None] = "b7e4d1c2a9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_conversations",
        sa.Column(
            "agent_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_conversations", "agent_paused")
