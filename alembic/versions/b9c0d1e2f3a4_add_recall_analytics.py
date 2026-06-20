"""add recall analytics — topics + neighbours

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-06-20 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # All three are populated offline by scripts/build_analytics.py. topic_id stays NULL until then;
    # the indexed column backs the `topic` filter.
    op.add_column("recalls", sa.Column("topic_id", sa.Integer(), nullable=True))
    op.create_index("ix_recalls_topic_id", "recalls", ["topic_id"])

    op.create_table(
        "recall_topics",
        # id is the assigned NMF component index, not autoincremented — matches recalls.topic_id.
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("top_terms", postgresql.JSONB(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
    )

    op.create_table(
        "recall_neighbors",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("recall_number", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("neighbor_source", sa.Text(), nullable=False),
        sa.Column("neighbor_number", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        # PK (source, recall_number, rank) also serves the lookup by recall, ordered by rank.
        sa.PrimaryKeyConstraint("source", "recall_number", "rank"),
    )


def downgrade() -> None:
    op.drop_table("recall_neighbors")
    op.drop_table("recall_topics")
    op.drop_index("ix_recalls_topic_id", table_name="recalls")
    op.drop_column("recalls", "topic_id")
