"""add recall trigram substring search

Adds a lowercased searchable-text column with a pg_trgm GIN index so substring/ILIKE searches
(partial UPCs, codes, word fragments) match — the tsvector search matches whole lexemes only,
so e.g. "882479" never matched a 12-digit UPC "882479852232".

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-06-30 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Lowercased searchable text. Kept identical to models._SEARCH_TEXT_LOWER_EXPR.
_SEARCH_TEXT_LOWER_EXPR = (
    "lower("
    "coalesce(product_description, '') || ' ' || "
    "coalesce(reason_text, '') || ' ' || "
    "coalesce(company_name, ''))"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.add_column(
        "recalls",
        sa.Column(
            "search_text",
            sa.Text(),
            sa.Computed(_SEARCH_TEXT_LOWER_EXPR, persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_recalls_search_text_trgm",
        "recalls",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_recalls_search_text_trgm", table_name="recalls")
    op.drop_column("recalls", "search_text")
