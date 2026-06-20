"""add country to recall_topics (per-country themes)

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-06-20 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Themes are now computed per country. recall_topics is fully rebuilt by build_analytics,
    # so this just needs the column to exist; any existing rows (transient) default to 'us'
    # until the next build replaces them. Indexed for the per-country `get_topics` lookup.
    op.add_column(
        "recall_topics",
        sa.Column("country", sa.Text(), nullable=False, server_default="us"),
    )
    op.create_index("ix_recall_topics_country", "recall_topics", ["country"])


def downgrade() -> None:
    op.drop_index("ix_recall_topics_country", table_name="recall_topics")
    op.drop_column("recall_topics", "country")
