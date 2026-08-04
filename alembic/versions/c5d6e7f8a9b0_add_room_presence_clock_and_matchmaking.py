"""add room presence, clock, outcome and matchmaking columns

Revision ID: c5d6e7f8a9b0
Revises: a0b1c2d3e4f5
Create Date: 2026-08-05 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "a0b1c2d3e4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Which seat opens, so either player can start and a rematch can swap the advantage.
    op.add_column(
        "rooms",
        sa.Column("first_seat", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # Findable by matchmaking.
    op.add_column(
        "rooms",
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # The move clock: a limit, and when the current turn started.
    op.add_column("rooms", sa.Column("move_limit_seconds", sa.Integer(), nullable=True))
    op.add_column("rooms", sa.Column("turn_started_at", sa.DateTime(timezone=True), nullable=True))
    # How the game ended and who took it.
    op.add_column("rooms", sa.Column("outcome", sa.String(length=16), nullable=True))
    op.add_column("rooms", sa.Column("winner_seat", sa.Integer(), nullable=True))

    op.create_check_constraint(
        "ck_rooms_outcome",
        "rooms",
        "outcome IS NULL OR outcome IN ('win','draw','timeout','forfeit')",
    )
    op.create_check_constraint("ck_rooms_first_seat", "rooms", "first_seat IN (0,1)")
    # Matchmaking takes the oldest room still open and waiting, for one game.
    op.create_index(
        "ix_rooms_open",
        "rooms",
        ["game_id", "created_at"],
        postgresql_where=sa.text("is_open AND status = 'waiting'"),
    )


def downgrade() -> None:
    op.drop_index("ix_rooms_open", table_name="rooms")
    op.drop_constraint("ck_rooms_first_seat", "rooms", type_="check")
    op.drop_constraint("ck_rooms_outcome", "rooms", type_="check")
    op.drop_column("rooms", "winner_seat")
    op.drop_column("rooms", "outcome")
    op.drop_column("rooms", "turn_started_at")
    op.drop_column("rooms", "move_limit_seconds")
    op.drop_column("rooms", "is_open")
    op.drop_column("rooms", "first_seat")
