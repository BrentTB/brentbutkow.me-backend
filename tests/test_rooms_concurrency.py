"""Rooms behaviour only a real Postgres can prove.

Everything here needs something SQLite and a stub session cannot give: `SELECT ... FOR UPDATE`,
`SKIP LOCKED`, `pg_advisory_xact_lock`, the `uq_rooms_code` unique index, the CHECK constraints, the
`jsonb_set` targeted update, and `jsonb_array_length`/containment in the matchmaking predicate. Set
TEST_DATABASE_URL to run (mirrors tests/test_subscriptions_uniqueness.py).

Two sessions on two connections in two threads is the whole point: a single-session test cannot
observe a row lock, because a transaction never blocks on itself.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.modules.rooms import constants, service
from app.modules.rooms.constants import MAX_SEATS, RoomStatus
from app.modules.rooms.models import Room

TEST_DB = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL (Postgres) to run the rooms concurrency tests"
)

GAME = "tic-tac-toe"
CELLS = 64
HOST_TOKEN = "host-token"
GUEST_TOKEN = "guest-token"


def _psycopg_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class _Clock:
    """The rooms clock, pinned, so a poll can be aged past a timeout without sleeping."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture(scope="module")
def engine():
    assert TEST_DB is not None
    engine = create_engine(_psycopg_url(TEST_DB))
    try:
        engine.connect().close()
    except OperationalError as exc:  # pragma: no cover - depends on local env
        pytest.skip(f"cannot reach TEST_DATABASE_URL: {exc}")
    # Only the rooms table — isolated, and it brings the CHECKs, the unique index and the partial
    # matchmaking index with it, which is exactly what these tests are here to exercise.
    Room.__table__.drop(engine, checkfirst=True)
    Room.__table__.create(engine)
    yield engine
    Room.__table__.drop(engine, checkfirst=True)
    engine.dispose()


@pytest.fixture
def sessions(engine):
    """Hands out independent sessions, each on its own connection, and cleans up after the test."""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    opened = []

    def open_session():
        session = factory()
        opened.append(session)
        return session

    yield open_session
    for session in opened:
        session.rollback()
        session.close()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM rooms"))


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> _Clock:
    pinned = _Clock(datetime.now(UTC))
    monkeypatch.setattr(service, "now_utc", pinned)
    monkeypatch.setattr(constants, "now_utc", pinned)
    return pinned


def _seat(seat: int, token: str, name: str = "Ada", colour: str = "1,2,3") -> dict:
    # The service's own minter, so the stored hash really matches the token these tests hold.
    return dict(service._seat_entry(seat, token, name, colour))


def _seed_room(session, *, code: str, seats: list[dict] | None = None, **over) -> Room:
    now = service.now_utc()
    base = dict(
        game_id=GAME,
        cell_count=CELLS,
        moves=[],
        seats=seats if seats is not None else [_seat(0, HOST_TOKEN)],
        status=RoomStatus.waiting,
        first_seat=0,
        owner_seat=0,
        is_open=False,
        move_limit_seconds=None,
        turn_started_at=None,
        outcome=None,
        winner_seat=None,
        expires_at=now + timedelta(seconds=settings.room_ttl_seconds),
    )
    base.update(over)
    room = Room(code=code, **base)
    session.add(room)
    return room


def _stored(session, code: str) -> Room:
    """The row as it is in the database, whatever any session already has cached."""
    return session.scalars(
        select(Room).where(Room.code == code).execution_options(populate_existing=True)
    ).one()


def _room_count(session) -> int:
    return session.scalar(select(func.count()).select_from(Room)) or 0


def _run_together(*calls):
    """Runs the calls at the same instant, one thread each, returning results in argument order.

    An HTTPException is returned rather than raised: which side loses a race is the thing under
    test, so the assertions have to see both outcomes.
    """
    barrier = threading.Barrier(len(calls))

    def run(call):
        barrier.wait()
        try:
            return call()
        except HTTPException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(run, call) for call in calls]
        return [future.result() for future in futures]


def _split(results):
    refused = [r for r in results if isinstance(r, HTTPException)]
    accepted = [r for r in results if not isinstance(r, HTTPException)]
    return accepted, refused


# --- two people arriving at once ---


def test_two_players_joining_at_once_get_one_seat_each(sessions):
    host = sessions()
    _seed_room(host, code="JOINAA")
    host.commit()

    first, second = sessions(), sessions()
    results = _run_together(
        lambda: service.join_room(first, code="JOINAA", name="Bo", colour="4,5,6"),
        lambda: service.join_room(second, code="JOINAA", name="Cy", colour="7,8,9"),
    )
    accepted, refused = _split(results)

    assert len(accepted) == 1
    assert [exc.status_code for exc in refused] == [409]
    assert refused[0].detail == "Room is full"

    _, token = accepted[0]
    stored = _stored(sessions(), "JOINAA")
    assert {int(entry["seat"]) for entry in stored.seats} == {0, 1}
    # Two seats, two different secrets, and the host's record was not overwritten by the joiner.
    assert len({entry["token_hash"] for entry in stored.seats}) == MAX_SEATS
    assert service.seat_for_token(stored.seats, HOST_TOKEN) == 0
    assert service.seat_for_token(stored.seats, token) == 1


def test_two_matchmakers_racing_for_one_seat_do_not_both_take_it(sessions):
    host = sessions()
    _seed_room(host, code="OPENAA", is_open=True)
    host.commit()

    first, second = sessions(), sessions()
    search = dict(game_id=GAME, cell_count=CELLS, colour="4,5,6")
    results = _run_together(
        lambda: service.matchmake(first, name="Bo", **search),
        lambda: service.matchmake(second, name="Cy", **search),
    )
    accepted, refused = _split(results)

    # SKIP LOCKED is what makes this two successes instead of one 409: the loser skips the row
    # somebody else is holding and opens a room of its own rather than queueing on it.
    assert refused == []
    codes = [room.code for room, _ in accepted]
    assert codes.count("OPENAA") == 1
    assert len(set(codes)) == 2

    stored = _stored(sessions(), "OPENAA")
    assert len(stored.seats) == MAX_SEATS
    assert service.seat_for_token(stored.seats, HOST_TOKEN) == 0


def test_matchmaking_puts_the_second_arrival_in_the_first_ones_room(sessions):
    search = dict(game_id=GAME, cell_count=CELLS, colour="4,5,6")
    opener, opener_token = service.matchmake(sessions(), name="Ada", **search)
    joiner, joiner_token = service.matchmake(sessions(), name="Bo", **search)

    assert joiner.code == opener.code
    assert _room_count(sessions()) == 1
    stored = _stored(sessions(), opener.code)
    assert service.seat_for_token(stored.seats, opener_token) == 0
    assert service.seat_for_token(stored.seats, joiner_token) == 1


def test_two_moves_at_the_same_version_leave_exactly_one_conflict(sessions):
    host = sessions()
    _seed_room(
        host,
        code="MOVEAA",
        seats=[_seat(0, HOST_TOKEN), _seat(1, GUEST_TOKEN, name="Bo")],
        status=RoomStatus.active,
        turn_started_at=service.now_utc(),
    )
    host.commit()

    first, second = sessions(), sessions()
    results = _run_together(
        lambda: service.append_move(
            first, code="MOVEAA", token=HOST_TOKEN, move=0, expected_version=0, finished=False
        ),
        lambda: service.append_move(
            second, code="MOVEAA", token=HOST_TOKEN, move=1, expected_version=0, finished=False
        ),
    )
    accepted, refused = _split(results)

    assert len(accepted) == 1
    assert [exc.status_code for exc in refused] == [409]
    assert refused[0].detail == "Move is out of date"
    # The row lock is the only reason the second submit saw the first one's move at all.
    assert len(_stored(sessions(), "MOVEAA").moves) == 1


# --- the targeted last_seen update ---


def test_a_touch_does_not_clobber_a_concurrent_join(sessions, clock):
    host = sessions()
    _seed_room(host, code="TOUCHA")
    host.commit()

    poller = sessions()
    # Held, not discarded: the identity map keeps only a weak reference, and letting this go
    # collected would hand the touch a freshly-loaded array instead of the stale one under test.
    snapshot = service.get_room(poller, "TOUCHA")
    service.join_room(sessions(), code="TOUCHA", name="Bo", colour="4,5,6")
    assert len(snapshot.seats) == 1  # the poller still sees the room as it was before the join

    clock.advance(service._TOUCH_INTERVAL_SECONDS + 1)
    service.touch_seat(poller, code="TOUCHA", token=HOST_TOKEN)

    stored = _stored(sessions(), "TOUCHA")
    # A touch writes one seat's own last_seen, so the seat that arrived in between is still there.
    assert {int(entry["seat"]) for entry in stored.seats} == {0, 1}
    assert service.seat_for_token(stored.seats, HOST_TOKEN) == 0
    host_entry = next(entry for entry in stored.seats if int(entry["seat"]) == 0)
    assert host_entry["last_seen"] == clock.now.isoformat()


@pytest.mark.parametrize("change", ["rename", "leave"])
def test_a_touch_does_not_revert_a_concurrent_change_to_the_other_seat(sessions, clock, change):
    host = sessions()
    _seed_room(
        host,
        code="TOUCHB",
        seats=[_seat(0, HOST_TOKEN), _seat(1, GUEST_TOKEN, name="Bo")],
    )
    host.commit()

    poller = sessions()
    snapshot = service.get_room(poller, "TOUCHB")  # held, so the touch runs off this stale array

    guest = sessions()
    if change == "rename":
        service.update_profile(
            guest, code="TOUCHB", token=GUEST_TOKEN, name="Renamed", colour="9,9,9"
        )
    else:
        service.leave_room(guest, code="TOUCHB", token=GUEST_TOKEN)

    stale_guest = next(entry for entry in snapshot.seats if int(entry["seat"]) == 1)
    assert (stale_guest["name"], stale_guest["joined"]) == ("Bo", True)  # snapshot predates it

    clock.advance(service._TOUCH_INTERVAL_SECONDS + 1)
    service.touch_seat(poller, code="TOUCHB", token=HOST_TOKEN)

    stored = _stored(sessions(), "TOUCHB")
    guest_entry = next(entry for entry in stored.seats if int(entry["seat"]) == 1)
    if change == "rename":
        assert guest_entry["name"] == "Renamed"
        assert guest_entry["colour"] == "9,9,9"
    else:
        assert guest_entry["joined"] is False


def test_a_touch_whose_seat_moved_is_a_no_op(sessions, clock):
    host = sessions()
    _seed_room(
        host,
        code="TOUCHC",
        seats=[_seat(0, HOST_TOKEN), _seat(1, GUEST_TOKEN, name="Bo")],
    )
    host.commit()

    poller = sessions()
    # Held: this snapshot, with the host's record at array position 0, is what the touch runs off.
    snapshot = service.get_room(poller, "TOUCHC")

    other = sessions()
    service.leave_room(other, code="TOUCHC", token=HOST_TOKEN)
    # The replacement is appended, so position 0 now holds seat 1's record, not the host's.
    service.join_room(other, code="TOUCHC", name="Cy", colour="7,8,9")
    before = _stored(sessions(), "TOUCHC")
    seats_before = [dict(entry) for entry in before.seats]
    expires_before = before.expires_at
    host_hash = _seat(0, HOST_TOKEN)["token_hash"]
    assert seats_before[0]["token_hash"] != host_hash
    assert snapshot.seats[0]["token_hash"] == host_hash  # but the stale snapshot still thinks so

    clock.advance(service._TOUCH_INTERVAL_SECONDS + 1)
    service.touch_seat(poller, code="TOUCHC", token=HOST_TOKEN)

    after = _stored(sessions(), "TOUCHC")
    # The token-hash guard failed, so the write stamped nothing rather than the wrong player.
    assert [dict(entry) for entry in after.seats] == seats_before
    assert after.expires_at == expires_before


# --- minting a code ---


def test_a_colliding_code_is_retried_rather_than_failing(sessions, monkeypatch):
    session = sessions()
    _seed_room(session, code="TAKENX")
    session.commit()

    codes = iter(["TAKENX", "FREEYZ"])
    monkeypatch.setattr(service, "random_code", lambda: next(codes))
    room, _ = service.create_room(
        session, game_id=GAME, name="Ada", colour="1,2,3", cell_count=CELLS
    )
    assert room.code == "FREEYZ"


def test_running_out_of_codes_is_a_503(sessions, monkeypatch):
    session = sessions()
    _seed_room(session, code="SAMEXX")
    session.commit()

    # Also the only exercise of the real pg_advisory_xact_lock(hashtext(...)) call, once per
    # attempt.
    monkeypatch.setattr(service, "random_code", lambda: "SAMEXX")
    with pytest.raises(HTTPException) as exc:
        service.create_room(session, game_id=GAME, name="Ada", colour="1,2,3", cell_count=CELLS)
    assert exc.value.status_code == 503
    session.rollback()
    assert _room_count(sessions()) == 1


def test_the_unique_index_rejects_two_rooms_with_one_code(sessions):
    first = sessions()
    _seed_room(first, code="DUPEXX")
    first.commit()

    second = sessions()
    _seed_room(second, code="DUPEXX")
    with pytest.raises(IntegrityError):
        second.commit()
    second.rollback()
    assert _room_count(sessions()) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "bogus"),
        ("outcome", "bogus"),
        ("first_seat", 2),
        ("owner_seat", 2),
        ("winner_seat", 2),
    ],
)
def test_a_value_the_service_could_never_write_is_refused_by_a_check(sessions, field, value):
    session = sessions()
    _seed_room(session, code="CHECKX", **{field: value})
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --- expiry ---


def test_an_expired_room_is_gone_on_read_and_pruned_on_the_next_create(sessions, clock):
    session = sessions()
    _seed_room(session, code="OLDXXX", expires_at=clock.now - timedelta(seconds=1))
    session.commit()

    with pytest.raises(HTTPException) as exc:
        service.get_room(session, "OLDXXX")
    assert exc.value.status_code == 410
    session.rollback()

    service.create_room(session, game_id=GAME, name="Ada", colour="1,2,3", cell_count=CELLS)
    assert session.scalar(select(func.count()).select_from(Room).where(Room.code == "OLDXXX")) == 0


# --- what the matchmaking query is allowed to offer ---


def test_matchmaking_takes_the_oldest_waiting_room(sessions, clock):
    session = sessions()
    _seed_room(session, code="OLDER1", is_open=True, created_at=clock.now - timedelta(minutes=10))
    _seed_room(session, code="NEWER1", is_open=True, created_at=clock.now - timedelta(minutes=1))
    session.commit()

    room, _ = service.matchmake(
        sessions(), game_id=GAME, cell_count=CELLS, name="Bo", colour="4,5,6"
    )
    assert room.code == "OLDER1"


def test_othello_matches_across_board_sizes(sessions):
    session = sessions()
    # An open 10x10 Othello room waiting; the newcomer asked for 8x8. Othello is size-agnostic to
    # match, so they join it and adopt its size rather than opening a second, lonelier room.
    _seed_room(session, code="OTHXXX", game_id="othello", cell_count=100, is_open=True)
    session.commit()

    room, _ = service.matchmake(sessions(), game_id="othello", cell_count=64, name="Bo", colour="0")
    assert room.code == "OTHXXX"
    assert room.cell_count == 100
    assert _room_count(sessions()) == 1


def test_a_size_specific_game_still_matches_only_its_own_size(sessions):
    session = sessions()
    # A game not in MATCH_ANY_SIZE_GAMES keeps size-specific pools: a different-sized room is not a
    # match, so this caller opens its own instead of joining.
    _seed_room(session, code="TTTBIG", game_id=GAME, cell_count=27, is_open=True)
    session.commit()

    room, _ = service.matchmake(
        sessions(), game_id=GAME, cell_count=CELLS, name="Bo", colour="4,5,6"
    )
    assert room.code != "TTTBIG"
    assert room.cell_count == CELLS
    assert _room_count(sessions()) == 2


def test_the_query_skips_a_full_room_and_finds_the_one_behind_it(sessions, clock):
    session = sessions()
    _seed_room(
        session,
        code="FULLXX",
        is_open=True,
        created_at=clock.now - timedelta(minutes=10),
        seats=[_seat(0, HOST_TOKEN), _seat(1, GUEST_TOKEN, name="Bo")],
    )
    _seed_room(session, code="WAITXX", is_open=True, created_at=clock.now - timedelta(minutes=1))
    session.commit()

    # Without the free-seat predicate the full lobby at the head of the queue starves everything
    # behind it, and this caller would open a third room instead of finding WAITXX.
    room, _ = service.matchmake(
        sessions(), game_id=GAME, cell_count=CELLS, name="Cy", colour="7,8,9"
    )
    assert room.code == "WAITXX"
    assert _room_count(sessions()) == 2


def test_a_room_somebody_left_is_offered_again(sessions):
    session = sessions()
    guest = _seat(1, GUEST_TOKEN, name="Bo")
    guest["joined"] = False
    _seed_room(session, code="LEFTXX", is_open=True, seats=[_seat(0, HOST_TOKEN), guest])
    session.commit()

    # Both records are still in the array, so only the left-marker containment check finds this
    # seat.
    room, token = service.matchmake(
        sessions(), game_id=GAME, cell_count=CELLS, name="Cy", colour="7,8,9"
    )
    assert room.code == "LEFTXX"
    assert service.seat_for_token(_stored(sessions(), "LEFTXX").seats, token) == 1


def test_the_candidate_scan_stops_at_its_cap(sessions, clock):
    session = sessions()
    stale = (clock.now - timedelta(seconds=settings.room_presence_timeout_seconds + 5)).isoformat()
    for index in range(service._MATCH_CANDIDATES + 1):
        ghost = _seat(0, f"ghost-{index}", name="Gone")
        ghost["last_seen"] = stale
        _seed_room(
            session,
            code=f"GHST{index:02d}",
            is_open=True,
            seats=[ghost],
            created_at=clock.now - timedelta(minutes=100 - index),
        )
    session.commit()

    room, _ = service.matchmake(
        sessions(), game_id=GAME, cell_count=CELLS, name="Bo", colour="4,5,6"
    )
    fresh = sessions()
    retired = fresh.scalar(
        select(func.count()).select_from(Room).where(Room.status == RoomStatus.abandoned)
    )
    # One scan retires at most a capful of ghosts; the rest wait for the next caller.
    assert retired == service._MATCH_CANDIDATES
    assert room.status == RoomStatus.waiting  # nothing to join, so this caller opened its own
