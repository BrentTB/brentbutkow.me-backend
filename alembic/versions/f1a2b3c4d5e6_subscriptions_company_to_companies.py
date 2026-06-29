"""subscriptions: company (text) -> companies (jsonb array)

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-07-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "companies",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Preserve any existing single company as a one-element array.
    op.execute(
        "UPDATE subscriptions SET companies = jsonb_build_array(company) "
        "WHERE company IS NOT NULL AND company <> ''"
    )
    op.drop_column("subscriptions", "company")


def downgrade() -> None:
    op.add_column("subscriptions", sa.Column("company", sa.Text(), nullable=True))
    # Collapse back to the first company (lossy — extra companies are dropped).
    op.execute(
        "UPDATE subscriptions SET company = companies->>0 WHERE jsonb_array_length(companies) > 0"
    )
    op.drop_column("subscriptions", "companies")
