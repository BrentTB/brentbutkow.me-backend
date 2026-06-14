"""add fsis support: source, source_url, states, composite PK

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-14 19:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # source defaults existing rows to 'fda' before it joins the primary key.
    op.add_column("recalls", sa.Column("source", sa.Text(), nullable=False, server_default="fda"))
    op.add_column("recalls", sa.Column("source_url", sa.Text(), nullable=True))
    op.add_column("recalls", sa.Column("states", postgresql.JSONB(), nullable=True))
    op.drop_constraint("recalls_pkey", "recalls", type_="primary")
    op.create_primary_key("recalls_pkey", "recalls", ["source", "recall_number"])
    # Seed `states` for existing rows from the single `state`.
    op.execute("UPDATE recalls SET states = jsonb_build_array(state) WHERE state IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint("recalls_pkey", "recalls", type_="primary")
    op.create_primary_key("recalls_pkey", "recalls", ["recall_number"])
    op.drop_column("recalls", "states")
    op.drop_column("recalls", "source_url")
    op.drop_column("recalls", "source")
