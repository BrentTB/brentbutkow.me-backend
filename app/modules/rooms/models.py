from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Room(Base):
    """A turn-based match keyed by a short code, shared by two seats.

    Deliberately game-agnostic: ``game_id`` says which game, ``moves`` is the ordered list of wire
    integers that game exchanges, and ``cell_count`` bounds the board for the default legality
    check.
    Nothing here understands any game's rules — see ``validators.py``.
    """

    __tablename__ = "rooms"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting','active','finished','abandoned')",
            name="ck_rooms_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('win','draw','timeout','forfeit')",
            name="ck_rooms_outcome",
        ),
        CheckConstraint("first_seat IN (0,1)", name="ck_rooms_first_seat"),
        CheckConstraint("owner_seat IN (0,1)", name="ck_rooms_owner_seat"),
        Index("uq_rooms_code", "code", unique=True),
        # Read-time expiry and write-time pruning both scan by expiry.
        Index("ix_rooms_expires_at", "expires_at"),
        # Matchmaking takes the oldest room still open and waiting, for one game.
        Index(
            "ix_rooms_open",
            "game_id",
            "created_at",
            postgresql_where=text("is_open AND status = 'waiting'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(12), nullable=False)
    game_id: Mapped[str] = mapped_column(String(40), nullable=False)
    cell_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Ordered wire moves; a whole game replays from this list. Opaque to the server.
    moves: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # [{ seat, token_hash, name, colour, joined, last_seen }] — only each token's sha256 is
    # stored, and last_seen is an ISO timestamp of that seat's last read, which is how presence
    # is judged.
    seats: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="waiting")
    # Which seat opens the game. Turn is (first_seat + moves played) % 2, so either seat can
    # start and a rematch can hand the advantage over.
    first_seat: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Whose room it is: the seat that may change the settings and start a game. Seat 0 opens it,
    # and if that player walks out the seat still here inherits it, so a room is never left with
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
    # How the game ended and who took it. The server sets these itself on a timeout or a
    # forfeit, and records what the client reports for an ordinary win or draw.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    winner_seat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
