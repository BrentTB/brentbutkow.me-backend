"""rooms: a second matchmaking index for the any-size (cross-board) pool

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-08-07 00:00:00.000000

Othello matchmaking searches across board sizes, so it drops the ``cell_count`` predicate and
orders by ``created_at``. ``ix_rooms_open`` leads with ``(game_id, cell_count, ...)``, so without
the equality on ``cell_count`` Postgres cannot use it for the ordered scan and sorts the whole
open-Othello backlog instead. This adds a ``(game_id, created_at)`` partial index sharing the same
predicate, so that scan stays an ordered seek.

The literals here are kept in step with ``app/modules/rooms/constants.py`` by a sync test.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8a9b0c1d2e3"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ANY_SIZE_INDEX_COLUMNS = ["game_id", "created_at"]
_OPEN_INDEX_WHERE = "is_open AND status IN ('waiting','finished')"


def upgrade() -> None:
    op.create_index(
        "ix_rooms_open_any_size",
        "rooms",
        _ANY_SIZE_INDEX_COLUMNS,
        postgresql_where=sa.text(_OPEN_INDEX_WHERE),
    )


def downgrade() -> None:
    op.drop_index("ix_rooms_open_any_size", table_name="rooms")
