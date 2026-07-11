"""add subscription affected_countries (EU member-state narrowing)

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-07-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a4b5c6d7e8"
down_revision: str | None = "e2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # EU-scoped narrowing for a subscription: ISO alpha-2 member-state codes. Empty (the default,
    # backfilled onto existing rows) = every EU recall, so no current subscriber changes behaviour.
    op.add_column(
        "subscriptions",
        sa.Column(
            "affected_countries",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "affected_countries")
