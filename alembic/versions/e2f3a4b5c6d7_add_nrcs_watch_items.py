"""nrcs_watch_items: seen-statement state for the NRCS watcher

Revision ID: e2f3a4b5c6d7
Revises: b7c8d9e0f1a2
Create Date: 2026-07-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2f3a4b5c6d7"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every NRCS statement scripts/check_nrcs.py has seen, so a daily run can email the operator
    # only the new ones. Uniqueness on (source, item_key) goes in at creation (CLAUDE.md): item ids
    # are only unique within their SharePoint container.
    op.create_table(
        "nrcs_watch_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source", "item_key", name="uq_nrcs_watch_items_source_key"),
    )


def downgrade() -> None:
    op.drop_table("nrcs_watch_items")
