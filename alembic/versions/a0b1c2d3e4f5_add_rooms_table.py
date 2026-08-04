"""add rooms table

Revision ID: a0b1c2d3e4f5
Revises: b4c5d6e7f8a9
Create Date: 2026-08-03 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a0b1c2d3e4f5"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rooms",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("game_id", sa.String(length=40), nullable=False),
        sa.Column("cell_count", sa.Integer(), nullable=False),
        sa.Column("moves", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("seats", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'waiting'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('waiting','active','finished','abandoned')",
            name="ck_rooms_status",
        ),
    )
    op.create_index("uq_rooms_code", "rooms", ["code"], unique=True)
    op.create_index("ix_rooms_expires_at", "rooms", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_rooms_expires_at", table_name="rooms")
    op.drop_index("uq_rooms_code", table_name="rooms")
    op.drop_table("rooms")
