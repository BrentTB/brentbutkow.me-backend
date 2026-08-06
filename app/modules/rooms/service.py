from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast as as_type

from fastapi import HTTPException, status
from sqlalchemy import Text, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.rooms.constants import (
    MATCHABLE_STATUSES,
    MAX_SEATS,
    RoomOutcome,
    RoomStatus,
    SeatEntry,
    now_utc,
)
from app.modules.rooms.models import Room
from app.modules.rooms.validators import InvalidMove, Verdict, judge_outcome, validate_move

# Human-friendly room codes: no I/L/O/0/1, so a code read aloud or off a screen is unambiguous.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
_CODE_ATTEMPTS = 8
# How many open rooms a search looks at before opening one of its own.
_MATCH_CANDIDATES = 8
# 32 bytes → a 43-char urlsafe token; only its sha256 is stored (mirrors the subscription tokens).
_TOKEN_BYTES = 32
# A seat writes its last_seen at most this often, so a two-second poll is not a two-second write.
_TOUCH_INTERVAL_SECONDS = 5
# Matches a seats array holding a seat somebody pressed leave on, so SQL can spot a room with a
# free seat even when both records are still there.
_SEAT_LEFT_MARKER = [{"joined": False}]

# Games whose players are matched regardless of board size, so a small audience still finds a game.
# Every other game matches only within its own board size. A game listed here has to be one where a
# guest can simply adopt the size of the room they land in — Othello plays the same at any size.
# Keyed by game_id like the validator and outcome registries, so a game opts in without a wire flag.
MATCH_ANY_SIZE_GAMES = frozenset({"othello"})


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def random_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def other_seat(seat: int) -> int:
    return 1 - seat


def turn_seat(first_seat: int, moves: list[int]) -> int:
    """Whose turn it is: the opener, then alternating with each move played."""
    return (first_seat + len(moves)) % MAX_SEATS


def seat_for_token(seats: list[SeatEntry], token: str) -> int:
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


def _seat_quiet_past(entry: SeatEntry, now: datetime, timeout_seconds: int) -> bool:
    """Whether a seat has not read the room for longer than ``timeout_seconds``.

    A seat that has never read is still arriving, not quiet.
    """
    last_seen = _parse_iso(entry.get("last_seen"))
    if last_seen is None:
        return False
    return (now - last_seen).total_seconds() > timeout_seconds


def seat_present(entry: SeatEntry, now: datetime) -> bool:
    """
    Whether a seat still counts as occupied.

    A seat that left is marked, and one that stopped reading the room counts as gone too: a
    closed tab cannot announce itself, so silence past the timeout is the only signal there is.
    Judged on read, so noticing costs no write.

    The short window — a display signal, and what a between-games seat claim reads. Ending a live
    game needs the far wider ``_seat_forfeited``.
    """
    if not entry.get("joined", False):
        return False
    return not _seat_quiet_past(entry, now, settings.room_presence_timeout_seconds)


def _seat_forfeited(entry: SeatEntry, now: datetime) -> bool:
    """Whether a seat has been gone long enough to lose a game in progress.

    A much wider window than presence: a background tab's polling is throttled to roughly once a
    minute, so the silence that greys out an opponent's name is nowhere near proof they quit.
    """
    if not entry.get("joined", False):
        return True
    return _seat_quiet_past(entry, now, settings.room_forfeit_timeout_seconds)


def present_seats(seats: list[SeatEntry], now: datetime) -> list[SeatEntry]:
    return [entry for entry in seats if seat_present(entry, now)]


def _seat_records(seats: list[SeatEntry]) -> dict[int, SeatEntry]:
    return {int(entry["seat"]): entry for entry in seats}


def _clear_pending(seats: list[SeatEntry]) -> list[SeatEntry]:
    """The seats with any aimed-but-uncommitted move dropped — a new move settles the position."""
    cleared: list[SeatEntry] = []
    for entry in seats:
        if "pending_move" not in entry:
            cleared.append(entry)
            continue
        trimmed = dict(entry)
        trimmed.pop("pending_move", None)
        cleared.append(as_type(SeatEntry, trimmed))
    return cleared


def _seat_pending(seats: list[SeatEntry], seat: int) -> int | None:
    """The move a seat has aimed but not committed, or None."""
    entry = _seat_records(seats).get(seat)
    move = entry.get("pending_move") if entry is not None else None
    return int(move) if move is not None else None


def free_seat(seats: list[SeatEntry], now: datetime) -> int | None:
    """The lowest seat number nobody is sitting in, or None when the room is full.

    Presence only — a display signal. What a new player may take is ``_claimable_seat``.
    """
    taken = {int(entry["seat"]) for entry in present_seats(seats, now)}
    for index in range(MAX_SEATS):
        if index not in taken:
            return index
    return None


def _claimable_seat(seats: list[SeatEntry], now: datetime, *, room_status: str) -> int | None:
    """The seat a new arrival may take, or None when there is none to offer.

    A seat nobody ever took, and one whose player pressed leave, are both on offer. A seat that has
    only gone quiet is on offer between games and never during one: mid-game, silence is a phone
    asleep, and handing that seat to whoever has the code locks the player out of their own game
    with no way back — their token is the only proof of the seat, and replacing the record destroys
    it.
    """
    records = _seat_records(seats)
    for index in range(MAX_SEATS):
        entry = records.get(index)
        if entry is None or not entry.get("joined", False):
            return index
        if room_status != RoomStatus.active and not seat_present(entry, now):
            return index
    return None


def _absent_seat(room: Room, now: datetime) -> int | None:
    """The one seat that has been gone long enough to forfeit, or None when it is both or neither.

    Judged on the forfeit window, not presence: this decides a game, so it waits out a throttled
    tab rather than acting the moment a name greys out.
    """
    records = _seat_records(room.seats)
    absent = [
        seat
        for seat in range(MAX_SEATS)
        if seat not in records or _seat_forfeited(records[seat], now)
    ]
    return absent[0] if len(absent) == 1 else None


def turn_deadline(room: Room) -> datetime | None:
    """When the player on turn runs out of time, or None when the room has no clock running."""
    if room.status != RoomStatus.active or room.move_limit_seconds is None:
        return None
    if room.turn_started_at is None:
        return None
    started = room.turn_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return started + timedelta(seconds=room.move_limit_seconds)


def is_move_replay(
    *, moves: list[int], move: int, expected_version: int, first_seat: int, seat: int
) -> bool:
    """Whether this is the move the server already applied — a retry whose reply went missing.

    One version behind, the same cell, and played by this seat: nothing new is being asked for. The
    honest answer is the current state, not a 403, which reads identically to being thrown out of
    the room.
    """
    if not moves or expected_version != len(moves) - 1:
        return False
    if moves[-1] != move:
        return False
    return turn_seat(first_seat, moves[:-1]) == seat


def authorize_move(
    *,
    moves: list[int],
    seats: list[SeatEntry],
    cell_count: int,
    game_id: str,
    first_seat: int,
    token: str,
    move: int,
    expected_version: int,
) -> int:
    """Every rule an accepted move must satisfy, as pure logic (no DB) so it is directly testable.

    Order matters: identity, then freshness, then turn, then legality. Freshness comes before turn
    because a client on the wrong version is out of turn as a consequence, and "your version is
    stale" is both the more specific answer and a retriable one — where 403 is what a player who no
    longer holds the seat gets, and cannot be retried into working. Returns the mover's seat.
    """
    seat = seat_for_token(seats, token)
    if expected_version != len(moves):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Move is out of date")
    if turn_seat(first_seat, moves) != seat:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your turn")
    try:
        validate_move(game_id, moves, move, seat, cell_count)
    except InvalidMove as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return seat


def _expiry(now: datetime) -> datetime:
    return now + timedelta(seconds=settings.room_ttl_seconds)


def _prune_expired(session: Session) -> None:
    # Opportunistic cleanup on write, so expired rooms free their codes without a scheduled job.
    # Rides on the caller's transaction; the caller's commit is what makes it stick.
    session.execute(delete(Room).where(Room.expires_at <= now_utc()))


def _unique_code(session: Session) -> str:
    for _ in range(_CODE_ATTEMPTS):
        code = random_code()
        # Hold this candidate for the rest of the transaction, so two creators cannot both read it
        # as free and both insert it — that race escapes the unique index as a 500 rather than the
        # 503 this function raises when it truly runs out of codes.
        session.execute(select(func.pg_advisory_xact_lock(func.hashtext(code))))
        if session.scalar(select(Room.id).where(Room.code == code)) is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Could not allocate a room code, please retry.",
    )


def _seat_entry(seat: int, token: str, name: str, colour: str) -> SeatEntry:
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
        status=RoomStatus.waiting,
        first_seat=first_seat,
        owner_seat=0,
        is_open=is_open,
        move_limit_seconds=move_limit_seconds,
        expires_at=_expiry(now_utc()),
    )
    session.add(room)
    session.commit()
    session.refresh(room)
    return room, token


def _live_room(session: Session, code: str, *, lock: bool = False) -> Room:
    query = select(Room).where(Room.code == code)
    if lock:
        # populate_existing so a row already in the identity map is re-read under the lock, rather
        # than the lock being taken around values that predate it.
        query = query.with_for_update().execution_options(populate_existing=True)
    room = session.scalar(query)
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")
    if room.expires_at <= now_utc():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Room has expired")
    return room


def _play_timeout_move(session: Session, room: Room, seat: int, move: int) -> bool:
    """Plays a seat's aimed move because their clock ran out, or False if it is no longer legal.

    The board has not moved since the aim — only the seat on turn can change it — so this normally
    just appends. It is re-checked anyway so a pending that somehow went stale forfeits rather than
    lands. The move settles the position exactly as a submitted one does: the clock restarts for the
    player, the game's judge decides any result, and every seat's pending is cleared.
    """
    try:
        validate_move(room.game_id, room.moves, move, seat, room.cell_count)
    except InvalidMove:
        return False
    now = now_utc()
    room.moves = [*room.moves, move]
    room.turn_started_at = now
    room.expires_at = _expiry(now)
    room.seats = _clear_pending(room.seats)
    verdict = judge_outcome(
        room.game_id, moves=room.moves, first_seat=room.first_seat, cell_count=room.cell_count
    )
    if verdict is not None and verdict.finished:
        room.status = RoomStatus.finished
        room.outcome = RoomOutcome.win if verdict.winner_seat is not None else RoomOutcome.draw
        room.winner_seat = verdict.winner_seat
    session.flush()
    return True


def enforce_clock(session: Session, room: Room) -> Room:
    """
    Settles a game whose player on turn ran out of time.

    If that player had aimed a move but not committed it, the clock plays it for them and the game
    goes on — running out of time costs the turn, not the game. Otherwise the game is over, given to
    the player still waiting.

    Checked whenever the room is read or written rather than swept by a job: nobody is harmed by
    a timeout they have not looked at, and by the time either side asks, the answer is right.

    Flushes, never commits. A commit here would end the caller's transaction and drop the row lock
    it is holding, leaving everything after this line to run unlocked.
    """
    deadline = turn_deadline(room)
    if deadline is None or now_utc() <= deadline:
        return room
    loser = turn_seat(room.first_seat, room.moves)
    pending = _seat_pending(room.seats, loser)
    if pending is not None and _play_timeout_move(session, room, loser, pending):
        return room
    room.status = RoomStatus.finished
    room.outcome = RoomOutcome.timeout
    room.winner_seat = other_seat(loser)
    session.flush()
    return room


def enforce_presence(session: Session, room: Room) -> Room:
    """
    Ends a running game whose opponent has gone, awarding it to the player still here.

    A closed tab cannot forfeit, and a room with no clock has nothing else to end it: without this
    the game stays active until its TTL, offering no rematch, no settings, and one unblocked action
    — leaving, which records the wrong player as the one who walked out.

    A room both players have left is left alone: there is nobody to award it to, and its TTL
    collects it. Flushes, never commits, for the same reason as ``enforce_clock``.
    """
    if room.status != RoomStatus.active:
        return room
    absent = _absent_seat(room, now_utc())
    if absent is None:
        return room
    room.status = RoomStatus.finished
    room.outcome = RoomOutcome.forfeit
    room.winner_seat = other_seat(absent)
    session.flush()
    return room


def _needs_resolution(room: Room, now: datetime) -> bool:
    """Whether looking at this room decides a game — which is a write, and wants the row lock."""
    if room.status != RoomStatus.active:
        return False
    deadline = turn_deadline(room)
    if deadline is not None and now > deadline:
        return True
    return _absent_seat(room, now) is not None


def _live_room_checked(session: Session, code: str, *, lock: bool = False) -> Room:
    """The room with its game already settled: the clock and both players' presence resolved.

    Every read and every write comes through here, so a timeout or an opponent who vanished is
    decided before anything else reads the status — otherwise the next caller acts on a game the
    server already knows is over, and a leave records the timed-out player as the winner.

    An unlocked read that would decide something takes the lock and re-reads first: deciding is a
    write, and a write without the lock can land on top of a concurrent one.
    """
    room = _live_room(session, code, lock=lock)
    if not lock and _needs_resolution(room, now_utc()):
        room = _live_room(session, code, lock=True)
    enforce_clock(session, room)
    enforce_presence(session, room)
    return room


def get_room(session: Session, code: str) -> Room:
    room = _live_room_checked(session, code)
    session.commit()
    return room


def touch_seat(session: Session, *, code: str, token: str) -> Room:
    """
    Records that a seat is still reading the room, which is what keeps it counted as present.

    The hottest path in the API — both clients poll it every couple of seconds — so it writes
    narrowly rather than under a lock: one ``jsonb_set`` of this seat's own ``last_seen``, guarded
    on the token hash still sitting at that array position. Rewriting the whole seats array from a
    snapshot is what would lose a concurrent join, leave or rename, because it carries the other
    seat's stale values back with it.

    Throttled as well: the presence window is far wider than the gap this skips.
    """
    room = _live_room_checked(session, code)
    seat = seat_for_token(room.seats, token)
    now = now_utc()
    index = next((i for i, entry in enumerate(room.seats) if int(entry["seat"]) == seat), None)
    if index is None:  # pragma: no cover - seat_for_token found it, so it is there
        session.commit()
        return room
    entry = room.seats[index]
    last_seen = _parse_iso(entry.get("last_seen"))
    if last_seen is not None and (now - last_seen).total_seconds() < _TOUCH_INTERVAL_SECONDS:
        session.commit()
        return room

    session.flush()  # let any decided outcome land before the targeted update
    session.execute(
        update(Room)
        .where(
            Room.id == room.id,
            # If a concurrent write reshuffled the array, this position is somebody else's seat now
            # and the update quietly does nothing rather than stamping the wrong record.
            func.jsonb_extract_path_text(Room.seats, str(index), "token_hash")
            == entry.get("token_hash", ""),
        )
        .values(
            seats=func.jsonb_set(
                Room.seats,
                cast(array([str(index), "last_seen"]), ARRAY(Text)),
                func.to_jsonb(cast(now.isoformat(), Text)),
            ),
            # A room dies of idleness, not of age: a poll is proof somebody is still in it.
            expires_at=_expiry(now),
            updated_at=now,
        )
    )
    session.commit()
    session.refresh(room)
    return room


def _clear_last_game(room: Room) -> None:
    """Puts a finished room back to waiting, with nothing of the last game left on the board."""
    room.status = RoomStatus.waiting
    room.moves = []
    room.outcome = None
    room.winner_seat = None
    room.turn_started_at = None
    room.seats = _clear_pending(room.seats)


def _claim_seat(session: Session, room: Room, *, name: str, colour: str) -> tuple[Room, str]:
    """Seats a player, and hands them the room when nobody who could run it is here.

    Starts nothing: filling the second seat makes the room ready, and somebody still has to press
    start, which is what keeps the settings open to change until then.

    A room whose last game is over clears as the new player sits down, so nobody arrives to find
    somebody else's finished board.
    """
    now = now_utc()
    seat = _claimable_seat(room.seats, now, room_status=room.status)
    if seat is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A game is already running" if room.status == RoomStatus.active else "Room is full"
            ),
        )
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    # Reassign (not mutate) so SQLAlchemy tracks the JSONB change. A seat somebody left is replaced
    # rather than added to, so the room does not accumulate ghosts.
    kept = [entry for entry in room.seats if int(entry["seat"]) != seat]
    room.seats = [*kept, _seat_entry(seat, token, name, colour)]
    # A room whose owner is not here belongs to whoever just sat down — otherwise the settings and
    # the start button answer to an empty seat.
    owner = _seat_records(room.seats).get(room.owner_seat)
    if owner is None or not seat_present(owner, now):
        room.owner_seat = seat
    if room.status == RoomStatus.finished:
        _clear_last_game(room)
    room.expires_at = _expiry(now)
    session.commit()
    session.refresh(room)
    return room, token


def join_room(session: Session, *, code: str, name: str, colour: str) -> tuple[Room, str]:
    """Claim the room's open seat. Returns the room and the joiner's raw token."""
    room = _live_room_checked(session, code, lock=True)
    return _claim_seat(session, room, name=name, colour=colour)


def leave_room(session: Session, *, code: str, token: str) -> Room:
    """
    Gives up the caller's seat.

    Walking out of a game in progress hands it to the other player: the alternative is a room that
    waits forever for someone who has closed the page. Leaving before it starts, or after it has
    ended, just frees the seat, so the room can still be filled by somebody else.
    """
    room = _live_room_checked(session, code, lock=True)
    seat = seat_for_token(room.seats, token)
    now = now_utc()
    room.seats = [
        {**entry, "joined": False} if int(entry["seat"]) == seat else entry for entry in room.seats
    ]
    # The room outlives its opener, but only passes to somebody who is actually sitting here: handed
    # to an empty seat, the settings and the start button belong to nobody, and a player alone in
    # their own room cannot start a game in it.
    remaining = _seat_records(room.seats).get(other_seat(seat))
    if seat == room.owner_seat and remaining is not None and seat_present(remaining, now):
        room.owner_seat = other_seat(seat)
    if room.status == RoomStatus.active:
        room.status = RoomStatus.finished
        room.outcome = RoomOutcome.forfeit
        room.winner_seat = other_seat(seat)
    elif room.status == RoomStatus.waiting:
        room.turn_started_at = None
    session.commit()
    session.refresh(room)
    return room


def update_profile(session: Session, *, code: str, token: str, name: str, colour: str) -> Room:
    """Change the caller's own seat name and colour, so the opponent sees it on their next read."""
    room = _live_room_checked(session, code, lock=True)
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

    Only from the owner's seat, and only between games: mid-game the terms are settled, but before a
    game starts, including after one has finished, the owner may still change them.
    """
    room = _live_room_checked(session, code, lock=True)
    seat = seat_for_token(room.seats, token)
    if seat != room.owner_seat:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the room's owner can change this"
        )
    if room.status == RoomStatus.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A game is already running"
        )
    room.first_seat = first_seat
    room.is_open = is_open
    room.move_limit_seconds = move_limit_seconds
    session.commit()
    session.refresh(room)
    return room


def start_game(session: Session, *, code: str, token: str) -> Room:
    """
    Starts a game in a room that is ready for one, clearing whatever the last game left behind.

    The gate every game goes through, the first and the fifth alike. Nothing begins on its own, so
    the room's settings stay open to change right up to the moment start is pressed, and both
    players see the terms before a move is possible. The owner's call, since the terms are theirs.
    """
    room = _live_room_checked(session, code, lock=True)
    seat = seat_for_token(room.seats, token)
    if seat != room.owner_seat:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the room's owner can start a game"
        )
    now = now_utc()
    if len(present_seats(room.seats, now)) < MAX_SEATS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Both players have to be here"
        )
    if room.status == RoomStatus.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A game is already running"
        )
    _clear_last_game(room)
    room.status = RoomStatus.active
    room.turn_started_at = now
    room.expires_at = _expiry(now)
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
    Puts a player straight into a game: the longest-waiting open room with a seat, or a new one.

    The query asks for a free seat, not merely an open room — a handful of full lobbies sitting at
    the head of the queue would otherwise be all a search ever looks at, and every room behind them
    starves. A room whose last game finished is offered again too: the board clears as the new
    player sits down.

    `SKIP LOCKED` is what keeps two people arriving at once apart — each takes a different row
    instead of queueing on the same one — and the locks it takes are held to the end of the request,
    which is what makes the claim that follows safe.

    `first_seat` and `move_limit_seconds` only apply to a room this opens. Joining somebody means
    playing by the settings they already chose — including the board size, for a game in
    ``MATCH_ANY_SIZE_GAMES``, where a match across sizes beats no match at all. `cell_count` is
    still the size any room this opens is created at.
    """
    now = now_utc()
    filters = [
        Room.game_id == game_id,
        Room.is_open.is_(True),
        Room.status.in_(MATCHABLE_STATUSES),
        Room.expires_at > now,
        or_(
            func.jsonb_array_length(Room.seats) < MAX_SEATS,
            Room.seats.contains(_SEAT_LEFT_MARKER),
        ),
    ]
    if game_id not in MATCH_ANY_SIZE_GAMES:
        filters.append(Room.cell_count == cell_count)
    candidates = session.scalars(
        select(Room)
        .where(*filters)
        .order_by(Room.created_at.asc())
        .limit(_MATCH_CANDIDATES)
        .with_for_update(skip_locked=True)
    ).all()

    for room in candidates:
        # A waiting room whose host has gone quiet is a ghost: joining it means sitting alone in
        # somebody else's abandoned room, and because the oldest rooms are offered first, two people
        # looking at the same moment would each be sent to a different one. Retire it instead, so it
        # stops being offered at all. The retirement lands with whatever commits below.
        if not present_seats(room.seats, now):
            room.status = RoomStatus.abandoned
            continue
        if _claimable_seat(room.seats, now, room_status=room.status) is not None:
            return _claim_seat(session, room, name=name, colour=colour)

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
    """Validate and append a move under a row lock, so two racing submits can't both land.

    The result is the server's own reading wherever the game has a judge registered in
    ``validators.py``: for a placement game a completed line and a full board are both structurally
    checkable from the move list, so a client cannot declare a one-move win or end a live game by
    asking. **Trust boundary:** a game with no registered judge keeps the client's `finished`/`won`
    for it, and any seated player in such a game can end it — incoherent claims are still refused,
    but the verdict itself is only as honest as the client. Registering a judge closes that.
    """
    if won and not finished:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A winning move has to be a finishing move",
        )
    room = _live_room_checked(session, code, lock=True)
    seat = seat_for_token(room.seats, token)
    # Before the status check: a retry of the move that ended the game is still a retry, and the
    # state it asks for is exactly what the server has.
    if is_move_replay(
        moves=room.moves,
        move=move,
        expected_version=expected_version,
        first_seat=room.first_seat,
        seat=seat,
    ):
        session.commit()
        return room
    if room.status != RoomStatus.active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This game is over")
    authorize_move(
        moves=room.moves,
        seats=room.seats,
        cell_count=room.cell_count,
        game_id=room.game_id,
        first_seat=room.first_seat,
        token=token,
        move=move,
        expected_version=expected_version,
    )
    now = now_utc()
    room.moves = [*room.moves, move]
    room.turn_started_at = now
    room.expires_at = _expiry(now)
    # The position moved on, so any move a seat had aimed is stale — drop them all.
    room.seats = _clear_pending(room.seats)
    verdict = judge_outcome(
        room.game_id,
        moves=room.moves,
        first_seat=room.first_seat,
        cell_count=room.cell_count,
    )
    if verdict is None:
        verdict = Verdict(finished, seat if won else None)
    if verdict.finished:
        room.status = RoomStatus.finished
        room.outcome = RoomOutcome.win if verdict.winner_seat is not None else RoomOutcome.draw
        room.winner_seat = verdict.winner_seat
    session.commit()
    session.refresh(room)
    return room


def aim_move(session: Session, *, code: str, token: str, move: int, expected_version: int) -> Room:
    """Record a move a player has aimed but not committed, so the clock can play it on a timeout.

    Held to the same rules as a real move short of ending the turn: your seat, your turn, the right
    version, and a move legal in the position. Stored on your seat until you commit a move, aim a
    different one, or the clock runs out and plays it. Only the seat on turn can hold one.
    """
    room = _live_room_checked(session, code, lock=True)
    seat = seat_for_token(room.seats, token)
    if room.status != RoomStatus.active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This game is not running")
    if expected_version != len(room.moves):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Move is out of date")
    if turn_seat(room.first_seat, room.moves) != seat:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your turn")
    try:
        validate_move(room.game_id, room.moves, move, seat, room.cell_count)
    except InvalidMove as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    room.seats = [
        {**entry, "pending_move": move} if int(entry["seat"]) == seat else entry
        for entry in room.seats
    ]
    session.commit()
    session.refresh(room)
    return room
