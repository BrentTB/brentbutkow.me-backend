"""drop the recall trigram search index

The pg_trgm GIN index on recalls.search_text weighed ~66 MB against the ~10 MB of text it
indexed — the single largest object in the database — while serving only the substring fallback
of search. Search is now two-phase (service._needs_substring_search): the indexed tsvector match
runs first, and the substring ILIKE is added only when full-text finds too little, so the rare
fragment search seq-scans instead of every search paying for this index's storage. The generated
search_text column and the pg_trgm extension stay; restoring the index is the one CREATE INDEX
in downgrade().

Revision ID: b4c5d6e7f8a9
Revises: f3a4b5c6d7e8
Create Date: 2026-07-11 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_recalls_search_text_trgm", table_name="recalls")


def downgrade() -> None:
    op.create_index(
        "ix_recalls_search_text_trgm",
        "recalls",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
