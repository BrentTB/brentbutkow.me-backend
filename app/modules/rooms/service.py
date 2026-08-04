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
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
_CODE_ATTEMPTS = 8
# 32 bytes → a 43-char urlsafe token; only its sha256 is stored (mirrors the subscription tokens).
_TOKEN_BYTES = 32
MAX_SEATS = 2
# A seat writes its last_seen at most this often, so a two-second poll is not a two-second write.
_TOUCH_INTERVAL_SECONDS = 5

# How a game ended. The server decides the first two itself; the client reports the others.
Outcome_TIMEOUT = "timeout"
Outcome_FORFEIT = "forfeit"
Outcome_WIN = "win"
Outcome_DRAW = "draw"


def now_utc() -> datetime:
    return datetime.now(UTC)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def random_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def other_seat(seat: int) -> int:
    return 1 - seat


def turn_seat(first_seat: int, moves: list[int]) -> int:
    """Whose turn it is: the opener, then alternating with each move played."""
    return (first_seat + len(moves)) % MAX_SEATS


def seat_for_token(seats: list[dict], token: str) -> int:
    """Which seat the token belongs to, or 403 if it matches none. Constant-time comparison."""
    token_hash = _hash_token(token)
    for seat in seats:
        if hmac.compare_digest(seat.get("token_hash", ""), token_hash):
            return int(seat["seat"])
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a player in this room")


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def seat_present(entry: dict, now: datetime) -> bool:
    """
    Whether a seat still counts as occupied.

    A seat that left is marked, and one that stopped reading the room counts as gone too: a
    closed tab cannot announce itself, so silence past the timeout is the only signal there is.
    Judged on read, so noticing costs no write.
    """
    if not entry.get("joined", False):
        return False
    last_seen = _parse_iso(entry.get("last_seen"))
    if last_seen is None:
        return True  # a seat that has not read yet is still arriving, not gone
    return (now - last_seen).total_seconds() <= settings.room_presence_timeout_seconds


def present_seats(seats: list[dict], now: datetime) -> list[dict]:
    return [entry for entry in seats if seat_present(entry, now)]


def free_seat(seats: list[dict], now: datetime) -> int | None:
    """The lowest seat number nobody holds, or None when the room is full."""
    taken = {int(entry["seat"]) for entry in present_seats(seats, now)}
    for index in range(MAX_SEATS):
        if index not in taken:
            return index
    return None


def turn_deadline(room: Room) -> datetime | None:
    """When the player on turn runs out of time, or None when the room has no clock running."""
    if room.status != "active" or room.move_limit_seconds is None:
        return None
    if room.turn_started_at is None:
        return None
    started = room.turn_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return started + timedelta(seconds=room.move_limit_seconds)


def authorize_move(
    *,
    moves: list[int],
    seats: list[dict],
    cell_count: int,
    game_id: str,
    first_seat: int,
    token: str,
    move: int,
    expected_version: int,
) -> int:
    """Every rule an accepted move must satisfy, as pure logic (no DB) so it is directly testable.

    Order matters: identity, then turn, then freshness, then legality. Returns the mover's seat.
    """
    seat = seat_for_token(seats, token)
    version = len(moves)
    if turn_seat(first_seat, moves) != seat:
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
    session.execute(delete(Room).where(Room.expires_at <= now_utc()))
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
        "last_seen": now_utc().isoformat(),
    }


def create_room(
    session: Session,
    *,
    game_id: str,
    name: str,
    colour: str,
    cell_count: int,
    first_seat: int = 0,
    is_open: bool = False,
    move_limit_seconds: int | None = None,
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
        first_seat=first_seat,
        is_open=is_open,
        move_limit_seconds=move_limit_seconds,
        expires_at=now_utc() + timedelta(seconds=settings.room_ttl_seconds),
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
    if room.expires_at <= now_utc():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Room has expired")
    return room


def enforce_clock(session: Session, room: Room) -> Room:
    """
    Ends a game whose player on turn ran out of time, awarding it to the one still waiting.

    Checked whenever the room is read or written rather than swept by a job: nobody is harmed by
    a timeout they have not looked at, and by the time either side asks, the answer is right.
    """
    deadline = turn_deadline(room)
    if deadline is None or now_utc() <= deadline:
        return room
    loser = turn_seat(room.first_seat, room.moves)
    room.status = "finished"
    room.outcome = Outcome_TIMEOUT
    room.winner_seat = other_seat(loser)
    session.commit()
    session.refresh(room)
    return room


def get_room(session: Session, code: str) -> Room:
    return enforce_clock(session, _live_room(session, code))


def touch_seat(session: Session, *, code: str, token: str) -> Room:
    """
    Records that a seat is still reading the room, which is what keeps it counted as present.

    Throttled: a poll every couple of seconds would otherwise mean a write that often, and the
    presence window is far wider than the gap this skips.
    """
    room = enforce_clock(session, _live_room(session, code))
    seat = seat_for_token(room.seats, token)
    now = now_utc()
    entry = next((e for e in room.seats if int(e["seat"]) == seat), None)
    if entry is None:
        return room
    last_seen = _parse_iso(entry.get("last_seen"))
    if last_seen is not None and (now - last_seen).total_seconds() < _TOUCH_INTERVAL_SECONDS:
        return room
    room.seats = [
        {**e, "last_seen": now.isoformat()} if int(e["seat"]) == seat else e for e in room.seats
    ]
    session.commit()
    session.refresh(room)
    return room


def _claim_seat(session: Session, room: Room, *, name: str, colour: str) -> tuple[Room, str]:
    """Puts a player in the room's open seat and starts the game once both are filled."""
    now = now_utc()
    seat = free_seat(room.seats, now)
    if seat is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Room is full")
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    # Reassign (not mutate) so SQLAlchemy tracks the JSONB change. A seat somebody left is replaced
    # rather than added to, so the room does not accumulate ghosts.
    kept = [e for e in room.seats if int(e["seat"]) != seat]
    room.seats = [*kept, _seat_entry(seat, token, name, colour)]
    if len(present_seats(room.seats, now)) == MAX_SEATS:
        room.status = "active"
        room.turn_started_at = now
    session.commit()
    session.refresh(room)
    return room, token


def join_room(session: Session, *, code: str, name: str, colour: str) -> tuple[Room, str]:
    """Claim the room's open seat. Returns the room and the joiner's raw token."""
    room = enforce_clock(session, _live_room(session, code, lock=True))
    return _claim_seat(session, room, name=name, colour=colour)


def leave_room(session: Session, *, code: str, token: str) -> Room:
    """
    Gives up the caller's seat.

    Walking out of a game in progress hands it to the other player: the alternative is a room that
    waits forever for someone who has closed the page. Leaving before it starts just frees the seat,
    so the room can still be filled by somebody else.
    """
    room = _live_room(session, code, lock=True)
    seat = seat_for_token(room.seats, token)
    room.seats = [{**e, "joined": False} if int(e["seat"]) == seat else e for e in room.seats]
    if room.status == "active":
        room.status = "finished"
        room.outcome = Outcome_FORFEIT
        room.winner_seat = other_seat(seat)
    elif room.status == "waiting":
        room.turn_started_at = None
    session.commit()
    session.refresh(room)
    return room


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


def update_settings(
    session: Session,
    *,
    code: str,
    token: str,
    first_seat: int,
    is_open: bool,
    move_limit_seconds: int | None,
) -> Room:
    """
    Changes a waiting room's settings, for the player who opened it.

    Only before the game starts, and only from seat 0: once both players are in, the terms they
    agreed to are not one side's to rewrite.
    """
    room = _live_room(session, code, lock=True)
    seat = seat_for_token(room.seats, token)
    if seat != 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the room's opener can change this"
        )
    if room.status != "waiting":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="The game has already started"
        )
    room.first_seat = first_seat
    room.is_open = is_open
    room.move_limit_seconds = move_limit_seconds
    session.commit()
    session.refresh(room)
    return room


def rematch(session: Session, *, code: str, token: str) -> Room:
    """
    Starts another game between the same two players, with the other seat opening this time.

    Either player can call it, so nobody waits on an agreement neither side can see. Seats keep
    their names and colours, so only the board and the outcome reset.
    """
    room = _live_room(session, code, lock=True)
    seat_for_token(room.seats, token)  # players only
    now = now_utc()
    if len(present_seats(room.seats, now)) < MAX_SEATS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Both players have to be here"
        )
    room.moves = []
    room.outcome = None
    room.winner_seat = None
    room.first_seat = other_seat(room.first_seat)
    room.status = "active"
    room.turn_started_at = now
    session.commit()
    session.refresh(room)
    return room


def matchmake(
    session: Session,
    *,
    game_id: str,
    cell_count: int,
    name: str,
    colour: str,
    first_seat: int = 0,
    move_limit_seconds: int | None = None,
) -> tuple[Room, str]:
    """
    Puts a player straight into a game: the longest-waiting open room, or a new open one.

    `SKIP LOCKED` is what makes two people arriving at once safe. Each takes a different row
    rather than queueing on the same one, so they never both claim the same seat.

    `first_seat` and `move_limit_seconds` only apply to a room this opens. Joining somebody means
    playing by the settings they already chose.
    """
    now = now_utc()
    candidates = session.scalars(
        select(Room)
        .where(
            Room.game_id == game_id,
            Room.cell_count == cell_count,
            Room.is_open.is_(True),
            Room.status == "waiting",
            Room.expires_at > now,
        )
        .order_by(Room.created_at.asc())
        .limit(_CODE_ATTEMPTS)
        .with_for_update(skip_locked=True)
    ).all()

    retired = False
    for room in candidates:
        # A waiting room whose host has gone quiet is a ghost: joining it means sitting alone in
        # somebody else's abandoned room, and because the oldest rooms are offered first, two people
        # looking at the same moment would each be sent to a different one. Retire it instead, so it
        # stops being offered at all.
        if not present_seats(room.seats, now):
            room.status = "abandoned"
            retired = True
            continue
        if free_seat(room.seats, now) is not None:
            return _claim_seat(session, room, name=name, colour=colour)

    if retired:
        session.commit()

    # Nobody to match with, so this player becomes the one waiting — open, for the next arrival.
    return create_room(
        session,
        game_id=game_id,
        name=name,
        colour=colour,
        cell_count=cell_count,
        first_seat=first_seat,
        is_open=True,
        move_limit_seconds=move_limit_seconds,
    )


def append_move(
    session: Session,
    *,
    code: str,
    token: str,
    move: int,
    expected_version: int,
    finished: bool,
    won: bool = False,
) -> Room:
    """Validate and append a move under a row lock, so two racing submits can't both land."""
    room = enforce_clock(session, _live_room(session, code, lock=True))
    if room.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This game is over")
    seat = authorize_move(
        moves=room.moves,
        seats=room.seats,
        cell_count=room.cell_count,
        game_id=room.game_id,
        first_seat=room.first_seat,
        token=token,
        move=move,
        expected_version=expected_version,
    )
    room.moves = [*room.moves, move]
    room.turn_started_at = now_utc()
    if finished:
        room.status = "finished"
        # The client runs the rules and says whether that move won; the server records the verdict.
        room.outcome = Outcome_WIN if won else Outcome_DRAW
        room.winner_seat = seat if won else None
    session.commit()
    session.refresh(room)
    return room
