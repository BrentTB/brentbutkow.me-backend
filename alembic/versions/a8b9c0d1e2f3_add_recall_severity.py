"""add recall severity

Revision ID: a8b9c0d1e2f3
Revises: cffe2116d048
Create Date: 2026-06-20 14:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "cffe2116d048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows seed to 0 / 'low' via the server defaults; scripts/backfill_severity.py then
    # fills real values. New rows get both at ingest from the normalizers. The btree index on
    # severity_score backs `sort=severity` and the `minSeverity` filter.
    op.add_column(
        "recalls",
        sa.Column("severity_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "recalls",
        sa.Column("severity_label", sa.Text(), nullable=False, server_default="low"),
    )
    op.create_index("ix_recalls_severity_score", "recalls", ["severity_score"])


def downgrade() -> None:
    op.drop_index("ix_recalls_severity_score", table_name="recalls")
    op.drop_column("recalls", "severity_label")
    op.drop_column("recalls", "severity_score")
