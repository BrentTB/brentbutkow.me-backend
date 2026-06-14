"""add recall full-text search

Revision ID: b2c3d4e5f6a7
Revises: a1f2b3c4d5e6
Create Date: 2026-06-14 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1f2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Generated tsvector over the searchable text. Kept identical to models._SEARCH_EXPR.
_SEARCH_EXPR = (
    "to_tsvector('english', "
    "coalesce(product_description, '') || ' ' || "
    "coalesce(reason_text, '') || ' ' || "
    "coalesce(company_name, ''))"
)


def upgrade() -> None:
    op.add_column(
        "recalls",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR,
            sa.Computed(_SEARCH_EXPR, persisted=True),
            nullable=True,
        ),
    )
    op.create_index("ix_recalls_search", "recalls", ["search_vector"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_recalls_search", table_name="recalls")
    op.drop_column("recalls", "search_vector")
