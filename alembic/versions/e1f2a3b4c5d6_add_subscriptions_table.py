"""add subscriptions table

Revision ID: e1f2a3b4c5d6
Revises: 9f8e7d6c5b4a
Create Date: 2026-07-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "9f8e7d6c5b4a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending_confirmation'"),
        ),
        sa.Column(
            "entities",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("countries", postgresql.JSONB(), nullable=False),
        sa.Column(
            "categories",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("min_severity", sa.Text(), nullable=True),
        sa.Column("confirmation_token_hash", sa.Text(), nullable=True),
        sa.Column("management_token", sa.Text(), nullable=False),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "skipped_at",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.CheckConstraint(
            "status IN ('pending_confirmation', 'active', 'paused', 'unsubscribed')",
            name="ck_subscriptions_status",
        ),
        sa.UniqueConstraint("management_token", name="uq_subscriptions_management_token"),
    )

    op.create_index(
        "idx_subscriptions_email",
        "subscriptions",
        ["email"],
    )

    op.create_index(
        "idx_subscriptions_active",
        "subscriptions",
        ["id"],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("idx_subscriptions_active", table_name="subscriptions")
    op.drop_index("idx_subscriptions_email", table_name="subscriptions")
    op.drop_table("subscriptions")
