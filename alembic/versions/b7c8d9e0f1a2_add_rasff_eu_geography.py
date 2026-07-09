"""add RASFF EU geography columns

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-09 17:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # EU/RASFF geography (app/modules/recalls/rasff_eu.py) — NULL for every other source. RASFF is
    # ingested as one EU-wide country, so the member-state detail rides in these: the notifying
    # member state (ISO alpha-2) plus origin / distribution ISO-code lists. JSONB to match the
    # model's `states` pattern (none_as_null is an ORM-side setting, not DDL).
    op.add_column("recalls", sa.Column("notifying_country", sa.Text(), nullable=True))
    op.add_column("recalls", sa.Column("origin_countries", JSONB(), nullable=True))
    op.add_column("recalls", sa.Column("distribution_countries", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("recalls", "distribution_countries")
    op.drop_column("recalls", "origin_countries")
    op.drop_column("recalls", "notifying_country")
