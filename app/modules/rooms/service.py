from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.rooms.models import Room
from app.modules.rooms.validators import InvalidMove, validate_move

# Human-friendly room codes: no I/L/O/0/1, so a code read aloud or off a screen is unambiguous.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6
_CODE_ATTEMPTS = 8
# 32 bytes → a 43-char urlsafe token; only its sha256 is stored (mirrors the subscription tokens).
_TOKEN_BYTES = 32
_MAX_SEATS = 2


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def random_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def seat_for_token(seats: list[dict], token: str) -> int:
    """Which seat the token belongs to, or 403 if it matches none. Constant-time comparison."""
    token_hash = _hash_token(token)
    for seat in seats:
        if hmac.compare_digest(seat.get("token_hash", ""), token_hash):
            return int(seat["seat"])
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a player in this room")


def authorize_move(
    *,
    moves: list[int],
    seats: list[dict],
    cell_count: int,
    game_id: str,
    token: str,
    move: int,
    expected_version: int,
) -> int:
    """Every rule an accepted move must satisfy, as pure logic (no DB) so it is directly testable.

    Order matters: identity, then turn, then freshness, then legality. Returns the mover's seat.
    """
    seat = seat_for_token(seats, token)
    version = len(moves)
    if version % _MAX_SEATS != seat:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your turn")
    if expected_version != version:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Move is out of date")
    try:
        validate_move(game_id, moves, move, seat, cell_count)
    except InvalidMove as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return seat


def _prune_expired(session: Session) -> None:
    # Opportunistic cleanup on write, so expired rooms free their codes without a scheduled job.
    session.execute(delete(Room).where(Room.expires_at <= _now()))
    session.commit()


def _unique_code(session: Session) -> str:
    for _ in range(_CODE_ATTEMPTS):
        code = random_code()
        if session.scalar(select(Room.id).where(Room.code == code)) is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Could not allocate a room code, please retry.",
    )


def _seat_entry(seat: int, token: str, name: str, colour: str) -> dict:
    return {
        "seat": seat,
        "token_hash": _hash_token(token),
        "name": name,
        "colour": colour,
        "joined": True,
    }


def create_room(
    session: Session, *, game_id: str, name: str, colour: str, cell_count: int
) -> tuple[Room, str]:
    """Open a room; the creator takes seat 0. Returns the room and the creator's raw token."""
    _prune_expired(session)
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    room = Room(
        code=_unique_code(session),
        game_id=game_id,
        cell_count=cell_count,
        moves=[],
        seats=[_seat_entry(0, token, name, colour)],
        status="waiting",
        expires_at=_now() + timedelta(seconds=settings.room_ttl_seconds),
    )
    session.add(room)
    session.commit()
    session.refresh(room)
    return room, token


def _live_room(session: Session, code: str, *, lock: bool = False) -> Room:
    query = select(Room).where(Room.code == code)
    if lock:
        query = query.with_for_update()
    room = session.scalar(query)
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")
    if room.expires_at <= _now():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Room has expired")
    return room


def get_room(session: Session, code: str) -> Room:
    return _live_room(session, code)


def join_room(session: Session, *, code: str, name: str, colour: str) -> tuple[Room, str]:
    """Claim the open second seat. Returns the room and the joiner's raw token."""
    room = _live_room(session, code, lock=True)
    if any(int(seat["seat"]) == 1 for seat in room.seats):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Room is full")
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    # Reassign (not mutate) so SQLAlchemy tracks the JSONB change.
    room.seats = [*room.seats, _seat_entry(1, token, name, colour)]
    room.status = "active"
    session.commit()
    session.refresh(room)
    return room, token


def update_profile(session: Session, *, code: str, token: str, name: str, colour: str) -> Room:
    """Change the caller's own seat name and colour, so the opponent sees it on their next read."""
    room = _live_room(session, code, lock=True)
    seat = seat_for_token(room.seats, token)
    room.seats = [
        {**entry, "name": name, "colour": colour} if int(entry["seat"]) == seat else entry
        for entry in room.seats
    ]
    session.commit()
    session.refresh(room)
    return room


def append_move(
    session: Session,
    *,
    code: str,
    token: str,
    move: int,
    expected_version: int,
    finished: bool,
) -> Room:
    """Validate and append a move under a row lock, so two racing submits can't both land."""
    room = _live_room(session, code, lock=True)
    authorize_move(
        moves=room.moves,
        seats=room.seats,
        cell_count=room.cell_count,
        game_id=room.game_id,
        token=token,
        move=move,
        expected_version=expected_version,
    )
    room.moves = [*room.moves, move]
    if finished:
        room.status = "finished"
    session.commit()
    session.refresh(room)
    return room
