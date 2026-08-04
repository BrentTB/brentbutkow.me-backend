"""add room owner seat

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-08-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: str | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("owner_seat", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint("ck_rooms_owner_seat", "rooms", "owner_seat IN (0,1)")


def downgrade() -> None:
    op.drop_constraint("ck_rooms_owner_seat", "rooms", type_="check")
    op.drop_column("rooms", "owner_seat")
