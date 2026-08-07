from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.modules.rooms.constants import (
    ROOM_FIRST_SEAT_CHECK,
    ROOM_OPEN_ANY_SIZE_INDEX_COLUMNS,
    ROOM_OPEN_INDEX_COLUMNS,
    ROOM_OPEN_INDEX_WHERE,
    ROOM_OUTCOME_CHECK,
    ROOM_OWNER_SEAT_CHECK,
    ROOM_STATUS_CHECK,
    ROOM_WINNER_SEAT_CHECK,
    RoomStatus,
    SeatEntry,
    now_utc,
)


class Room(Base):
    """A turn-based match keyed by a short code, shared by two seats.

    Deliberately game-agnostic: ``game_id`` says which game, ``moves`` is the ordered list of wire
    integers that game exchanges, and ``cell_count`` bounds the board for the default legality
    check.
    Nothing here understands any game's rules — see ``validators.py``.
    """

    __tablename__ = "rooms"
    __table_args__ = (
        CheckConstraint(ROOM_STATUS_CHECK, name="ck_rooms_status"),
        CheckConstraint(ROOM_OUTCOME_CHECK, name="ck_rooms_outcome"),
        CheckConstraint(ROOM_FIRST_SEAT_CHECK, name="ck_rooms_first_seat"),
        CheckConstraint(ROOM_OWNER_SEAT_CHECK, name="ck_rooms_owner_seat"),
        CheckConstraint(ROOM_WINNER_SEAT_CHECK, name="ck_rooms_winner_seat"),
        Index("uq_rooms_code", "code", unique=True),
        # Read-time expiry and write-time pruning both scan by expiry.
        Index("ix_rooms_expires_at", "expires_at"),
        # Matchmaking takes the oldest matchable open room, for one game and board size.
        Index(
            "ix_rooms_open",
            *ROOM_OPEN_INDEX_COLUMNS,
            postgresql_where=text(ROOM_OPEN_INDEX_WHERE),
        ),
        # Games in MATCH_ANY_SIZE_GAMES search without the cell_count predicate, so they need the
        # created_at order to fall directly out of the index rather than a sort of every open room.
        Index(
            "ix_rooms_open_any_size",
            *ROOM_OPEN_ANY_SIZE_INDEX_COLUMNS,
            postgresql_where=text(ROOM_OPEN_INDEX_WHERE),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Wider than the six characters the server mints and the router accepts, which leaves room for a
    # longer or prefixed code without a migration.
    code: Mapped[str] = mapped_column(String(12), nullable=False)
    game_id: Mapped[str] = mapped_column(String(40), nullable=False)
    cell_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Ordered wire moves; a whole game replays from this list. Opaque to the transport.
    moves: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # One record per seat — see ``SeatEntry``. Presence is judged from ``last_seen`` on read.
    seats: Mapped[list[SeatEntry]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RoomStatus.waiting,
        server_default=text(f"'{RoomStatus.waiting.value}'"),
    )
    # Which seat opens the game. Turn is (first_seat + moves played) % 2, so either seat can
    # start and a rematch can hand the advantage over.
    first_seat: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Whose room it is: the seat that may change the settings and start a game. Seat 0 opens it, and
    # it moves on only to a seat somebody is actually sitting in, so a room is never left with
    # nobody able to start it.
    owner_seat: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Whether matchmaking may hand this room to a stranger looking for a game.
    is_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # How long a player has to move before losing on time. Null means no clock.
    move_limit_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When the current turn began, so the clock has something to count from.
    turn_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How the game ended and who took it. The server decides these — on a timeout, a walk-out, an
    # opponent who vanished, and for any game with a registered outcome judge.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    winner_seat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now_utc, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=now_utc,
        onupdate=now_utc,
        server_default=func.now(),
    )
    # Measures idleness, not age: every write pushes it out by the room TTL.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
