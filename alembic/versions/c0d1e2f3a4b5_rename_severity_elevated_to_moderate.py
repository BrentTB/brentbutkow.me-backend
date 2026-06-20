"""rename severity label elevated to moderate

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-06-20 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0d1e2f3a4b5"
down_revision: str | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Pure label rename — the 35–55 score band is unchanged, so every row currently 'elevated'
    # becomes 'moderate'. Runs on deploy before the app serves, so reads never hit the old value
    # against the new SeverityLabel enum (which would fail RecallOut validation). The column's
    # server default is 'low', so it needs no change.
    op.execute("UPDATE recalls SET severity_label = 'moderate' WHERE severity_label = 'elevated'")


def downgrade() -> None:
    op.execute("UPDATE recalls SET severity_label = 'elevated' WHERE severity_label = 'moderate'")
