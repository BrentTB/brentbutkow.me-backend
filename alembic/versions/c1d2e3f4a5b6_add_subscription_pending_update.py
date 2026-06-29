"""subscriptions: add pending_update column

Revision ID: c1d2e3f4a5b6
Revises: f1a2b3c4d5e6
Create Date: 2026-07-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Staged preference change for a confirmed subscriber, pending email confirmation. Nullable —
    # null means no change is in flight.
    op.add_column(
        "subscriptions",
        sa.Column("pending_update", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "pending_update")
