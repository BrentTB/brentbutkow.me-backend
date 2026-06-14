"""add recall indexes

Revision ID: a1f2b3c4d5e6
Revises: 4df6eda8bdfa
Create Date: 2026-06-14 11:25:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f2b3c4d5e6"
down_revision: str | None = "4df6eda8bdfa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # report_date backs the default ordering, the `since` filter, and the monthly stats grouping;
    # category backs the category filter and the per-category stats grouping. Names match the
    # model's index=True defaults (ix_<table>_<column>) so autogenerate stays a no-op.
    op.create_index("ix_recalls_report_date", "recalls", ["report_date"])
    op.create_index("ix_recalls_category", "recalls", ["category"])


def downgrade() -> None:
    op.drop_index("ix_recalls_category", table_name="recalls")
    op.drop_index("ix_recalls_report_date", table_name="recalls")
