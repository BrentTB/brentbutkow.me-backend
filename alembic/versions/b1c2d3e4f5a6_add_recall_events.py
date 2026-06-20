"""add recall_events (event/outbreak clusters) + recalls.event_cluster_id

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
Create Date: 2026-06-20 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a9b8c7d6e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Recall clusters, fully rebuilt by scripts/build_events.py — so this just needs the table to
    # exist; the next build populates it. Mirrors recall_topics: per-country surrogate ids + a
    # stable slug the API filters by.
    op.create_table(
        "recall_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("country", sa.Text(), nullable=False, server_default="us"),
        sa.Column("slug", sa.Text(), nullable=False, server_default=""),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("is_outbreak", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dominant_entity", sa.Text(), nullable=True),
        sa.Column("recall_count", sa.Integer(), nullable=False),
        sa.Column("company_count", sa.Integer(), nullable=False),
        sa.Column("state_count", sa.Integer(), nullable=False),
        sa.Column("first_date", sa.Date(), nullable=True),
        sa.Column("last_date", sa.Date(), nullable=True),
        sa.Column("severity_max", sa.Float(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_recall_events_country", "recall_events", ["country"])
    op.create_index("ix_recall_events_slug", "recall_events", ["slug"])

    # The cluster each recall belongs to (recall_events.id); distinct from the FDA `event_id`.
    op.add_column("recalls", sa.Column("event_cluster_id", sa.Integer(), nullable=True))
    op.create_index("ix_recalls_event_cluster_id", "recalls", ["event_cluster_id"])


def downgrade() -> None:
    op.drop_index("ix_recalls_event_cluster_id", table_name="recalls")
    op.drop_column("recalls", "event_cluster_id")
    op.drop_index("ix_recall_events_slug", table_name="recall_events")
    op.drop_index("ix_recall_events_country", table_name="recall_events")
    op.drop_table("recall_events")
