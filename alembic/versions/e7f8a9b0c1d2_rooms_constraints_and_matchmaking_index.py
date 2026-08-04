"""rooms: winner_seat check, timestamp defaults, and a matchmaking index that matches the query

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-08-04 14:20:11.482913

Three gaps between the model and the database:

* ``winner_seat`` was the only seat column with no CHECK.
* ``created_at``/``updated_at`` had no DB default, so a non-ORM INSERT failed on NOT NULL.
* ``ix_rooms_open`` indexed ``(game_id, created_at)`` while matchmaking also filters on
  ``cell_count``, and its predicate only admitted ``waiting`` — a finished room never came back
  into the pool.

The literals here are kept in step with ``app/modules/rooms/constants.py`` by a sync test.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN_INDEX_COLUMNS = ["game_id", "cell_count", "created_at"]
_OPEN_INDEX_WHERE = "is_open AND status IN ('waiting','finished')"
_WINNER_SEAT_CHECK = "winner_seat IS NULL OR winner_seat IN (0,1)"

# What this revision replaces, restored on downgrade.
_PREVIOUS_OPEN_INDEX_COLUMNS = ["game_id", "created_at"]
_PREVIOUS_OPEN_INDEX_WHERE = "is_open AND status = 'waiting'"


def upgrade() -> None:
    op.create_check_constraint("ck_rooms_winner_seat", "rooms", _WINNER_SEAT_CHECK)
    op.alter_column(
        "rooms",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=sa.func.now(),
    )
    op.alter_column(
        "rooms",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=sa.func.now(),
    )
    op.drop_index("ix_rooms_open", table_name="rooms")
    op.create_index(
        "ix_rooms_open",
        "rooms",
        _OPEN_INDEX_COLUMNS,
        postgresql_where=sa.text(_OPEN_INDEX_WHERE),
    )


def downgrade() -> None:
    op.drop_index("ix_rooms_open", table_name="rooms")
    op.create_index(
        "ix_rooms_open",
        "rooms",
        _PREVIOUS_OPEN_INDEX_COLUMNS,
        postgresql_where=sa.text(_PREVIOUS_OPEN_INDEX_WHERE),
    )
    op.alter_column(
        "rooms",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=None,
    )
    op.alter_column(
        "rooms",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
        server_default=None,
    )
    op.drop_constraint("ck_rooms_winner_seat", "rooms", type_="check")
