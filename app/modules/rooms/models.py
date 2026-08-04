from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String
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
        Index("uq_rooms_code", "code", unique=True),
        # Read-time expiry and write-time pruning both scan by expiry.
        Index("ix_rooms_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(12), nullable=False)
    game_id: Mapped[str] = mapped_column(String(40), nullable=False)
    cell_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Ordered wire moves; a whole game replays from this list. Opaque to the server.
    moves: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # [{ seat, token_hash, colour, joined }] — only the sha256 of each seat's token is stored.
    seats: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="waiting")
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
