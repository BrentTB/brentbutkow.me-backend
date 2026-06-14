"""add country column

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-14 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows are all openFDA/FSIS → US (server_default backfills them).
    op.add_column("recalls", sa.Column("country", sa.Text(), nullable=False, server_default="us"))
    op.create_index("ix_recalls_country", "recalls", ["country"])


def downgrade() -> None:
    op.drop_index("ix_recalls_country", table_name="recalls")
    op.drop_column("recalls", "country")
