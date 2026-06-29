"""dispatch_state: persist the dispatch cursor across restarts

Revision ID: a2b3c4d5e6f7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Singleton row (id=1) holding the dispatch cursor so a restart/deploy doesn't re-treat the
    # whole recall backlog as new.
    op.create_table(
        "dispatch_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("dispatch_state")
