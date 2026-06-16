"""add recall entities

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-16 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows backfill to [] via the server default; scripts/backfill_entities.py then fills
    # real values. GIN index backs the `@>` entity filter + the by-entity unnest aggregation.
    op.add_column(
        "recalls",
        sa.Column(
            "entities",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_index("ix_recalls_entities", "recalls", ["entities"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_recalls_entities", table_name="recalls")
    op.drop_column("recalls", "entities")
