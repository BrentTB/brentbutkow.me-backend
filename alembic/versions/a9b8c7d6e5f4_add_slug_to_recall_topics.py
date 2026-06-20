"""add slug to recall_topics (stable theme key)

Revision ID: a9b8c7d6e5f4
Revises: d1e2f3a4b5c6
Create Date: 2026-06-20 21:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9b8c7d6e5f4"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Stable per-country theme key the API filters by, so a bookmarked theme survives a rebuild
    # (unlike the surrogate id). recall_topics is fully rebuilt by build_analytics, so the column
    # just needs to exist; existing (transient) rows default to '' until the next build populates
    # real slugs. Indexed for the slug → topic_id lookup.
    op.add_column(
        "recall_topics",
        sa.Column("slug", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_recall_topics_slug", "recall_topics", ["slug"])


def downgrade() -> None:
    op.drop_index("ix_recall_topics_slug", table_name="recall_topics")
    op.drop_column("recall_topics", "slug")
