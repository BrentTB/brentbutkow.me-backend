import importlib.util
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint

from app.config import settings
from app.db import get_session
from app.main import app
from app.modules.rooms import constants, service
from app.modules.rooms.constants import RoomStatus
from app.modules.rooms.models import Room
from app.modules.rooms.service import (
    CODE_ALPHABET,
    CODE_LENGTH,
    authorize_move,
    enforce_clock,
    free_seat,
    random_code,
    seat_for_token,
    seat_present,
    turn_deadline,
    turn_seat,
)
from app.modules.rooms.validators import (
    GAME_VALIDATORS,
    InvalidMove,
    default_placement_check,
    validate_move,
)

app.dependency_overrides[get_session] = lambda: None
client = TestClient(app)


def _room(**over) -> Room:
    base = dict(
        code="ABCDEF",
        game_id="tic-tac-toe",
        cell_count=64,
        moves=[],
        seats=[{"seat": 0, "token_hash": "h0", "name": "Ada", "colour": "1,2,3", "joined": True}],
        status="waiting",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        # An unsaved Room gets no column defaults, so the fixture spells them out.
        first_seat=0,
        owner_seat=0,
        is_open=False,
        move_limit_seconds=None,
        turn_started_at=None,
        outcome=None,
        winner_seat=None,
    )
    base.update(over)
    return Room(**base)


# --- routes (service stubbed, no DB) ---


def test_create_returns_seat_zero_and_token(monkeypatch):
    room = _room()
    monkeypatch.setattr(service, "create_room", lambda s, **kw: (room, "secret-token"))
    res = client.post(
        "/rooms",
        json={"gameId": "tic-tac-toe", "name": "Ada", "colour": "1,2,3", "cellCount": 64},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["seat"] == 0
    assert body["token"] == "secret-token"
    assert body["gameId"] == "tic-tac-toe"  # camelCase on the wire


def test_create_rejects_out_of_range_cell_count():
    res = client.post("/rooms", json={"gameId": "x", "colour": "c", "cellCount": 99999})
    assert res.status_code == 422


def test_state_never_exposes_seat_tokens(monkeypatch):
    room = _room(
        moves=[0, 1],
        status="active",
        seats=[
            {"seat": 0, "token_hash": "h0", "name": "Ada", "colour": "1,2,3", "joined": True},
            {"seat": 1, "token_hash": "h1", "name": "Bo", "colour": "4,5,6", "joined": True},
        ],
    )
    monkeypatch.setattr(service, "get_room", lambda s, code: room)
    res = client.get("/rooms/ABCDEF")
    assert res.status_code == 200
    body = res.json()
    assert body["version"] == 2
    assert body["moves"] == [0, 1]
    # The wire form of a seat carries no token, hashed or otherwise.
    assert set(body["seats"][0]) == {"seat", "name", "colour", "joined"}
    assert "tokenHash" not in body["seats"][0]
    # Each seat keeps its own name and colour, so the two players never render alike.
    assert [s["name"] for s in body["seats"]] == ["Ada", "Bo"]
    assert body["seats"][0]["colour"] != body["seats"][1]["colour"]


def test_move_forwards_fields_and_returns_state(monkeypatch):
    captured = {}

    def fake_append(session, **kw):
        captured.update(kw)
        return _room(moves=[5], status="active")

    monkeypatch.setattr(service, "append_move", fake_append)
    res = client.post(
        "/rooms/ABCDEF/move",
        json={"token": "t", "move": 5, "expectedVersion": 0, "finished": False},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["version"] == 1
    assert body["moves"] == [5]
    assert body["status"] == "active"
    assert captured["move"] == 5
    # camelCase expectedVersion on the wire arrives as snake_case in the service call.
    assert captured["expected_version"] == 0


def test_move_rejects_pass_sentinel_below_range():
    # -1 is the reserved pass value (max is fine for the schema), but -2 is out of the schema bound.
    res = client.post("/rooms/ABCDEF/move", json={"token": "t", "move": -2, "expectedVersion": 0})
    assert res.status_code == 422


def test_profile_update_forwards_the_callers_fields(monkeypatch):
    captured = {}

    def fake_update(session, **kw):
        captured.update(kw)
        return _room(status="active")

    monkeypatch.setattr(service, "update_profile", fake_update)
    res = client.post(
        "/rooms/ABCDEF/profile",
        json={"token": "tok", "name": "Ada", "colour": "9,9,9"},
    )
    assert res.status_code == 200
    assert captured == {"code": "ABCDEF", "token": "tok", "name": "Ada", "colour": "9,9,9"}


def test_profile_update_rejects_an_overlong_name():
    res = client.post(
        "/rooms/ABCDEF/profile",
        json={"token": "tok", "name": "x" * 200, "colour": "9,9,9"},
    )
    assert res.status_code == 422


# --- authorize_move: pure enforcement, no DB ---


def _seats_with_tokens(t0: str, t1: str) -> list[dict]:
    import hashlib

    def h(t):
        return hashlib.sha256(t.encode()).hexdigest()

    return [
        {"seat": 0, "token_hash": h(t0), "name": "Ada", "colour": "a", "joined": True},
        {"seat": 1, "token_hash": h(t1), "name": "Bo", "colour": "b", "joined": True},
    ]


def _authorize(**over):
    seats = over.pop("seats", _seats_with_tokens("tok0", "tok1"))
    base = dict(
        moves=[],
        seats=seats,
        cell_count=64,
        game_id="tic-tac-toe",
        first_seat=0,
        token="tok0",
        move=0,
        expected_version=0,
    )
    base.update(over)
    return authorize_move(**base)


def test_authorize_accepts_the_first_move():
    assert _authorize() == 0  # seat 0 moves first (version 0)


def test_authorize_rejects_unknown_token():
    with pytest.raises(HTTPException) as exc:
        _authorize(token="nope")
    assert exc.value.status_code == 403


def test_authorize_rejects_out_of_turn():
    # Seat 0 tries to move again at version 1, which is seat 1's turn.
    with pytest.raises(HTTPException) as exc:
        _authorize(moves=[0], token="tok0", move=1, expected_version=1)
    assert exc.value.status_code == 403


def test_authorize_rejects_stale_version():
    with pytest.raises(HTTPException) as exc:
        _authorize(expected_version=3)
    assert exc.value.status_code == 409


def test_authorize_rejects_occupied_cell():
    # Version 2 (seat 0's turn again), replaying an already-taken cell.
    with pytest.raises(HTTPException) as exc:
        _authorize(moves=[0, 1], token="tok0", move=0, expected_version=2)
    assert exc.value.status_code == 422


def test_authorize_rejects_out_of_range_cell():
    with pytest.raises(HTTPException) as exc:
        _authorize(move=64)  # cell_count is 64, so 64 is off the board
    assert exc.value.status_code == 422


# --- validators ---


def test_default_placement_rejects_pass_and_occupied():
    # Takes the full MoveValidator signature, seat included, so it is the registry's fallback value
    # rather than a special case.
    with pytest.raises(InvalidMove):
        default_placement_check([0, 1], -1, 0, 64)  # pass is not legal for a placement game
    with pytest.raises(InvalidMove):
        default_placement_check([0, 1], 1, 0, 64)  # occupied
    default_placement_check([0, 1], 2, 0, 64)  # a free in-range cell is fine


def test_registered_validator_is_used(monkeypatch):
    calls = {}

    def fake(moves, move, seat, cell_count):
        calls["hit"] = (list(moves), move, seat, cell_count)
        raise InvalidMove("nope")

    monkeypatch.setitem(GAME_VALIDATORS, "fake-game", fake)
    with pytest.raises(InvalidMove):
        validate_move("fake-game", [1], 2, 1, 64)
    assert calls["hit"] == ([1], 2, 1, 64)


# --- code generation ---


def test_random_code_shape():
    for _ in range(50):
        code = random_code()
        assert len(code) == CODE_LENGTH
        # No visually ambiguous characters.
        assert set(code) <= set(CODE_ALPHABET)


def test_seat_for_token_constant_lookup():
    seats = _seats_with_tokens("aaa", "bbb")
    assert seat_for_token(seats, "aaa") == 0
    assert seat_for_token(seats, "bbb") == 1
    with pytest.raises(HTTPException) as exc:
        seat_for_token(seats, "ccc")
    assert exc.value.status_code == 403


# --- turn order ---


def test_turn_alternates_from_whichever_seat_opens():
    # Seat 0 opening: even move counts are its turn.
    assert turn_seat(0, []) == 0
    assert turn_seat(0, [1]) == 1
    assert turn_seat(0, [1, 2]) == 0
    # Seat 1 opening: the parity flips, which is what a rematch hands over.
    assert turn_seat(1, []) == 1
    assert turn_seat(1, [1]) == 0
    assert turn_seat(1, [1, 2]) == 1


def test_authorize_rejects_the_opener_when_the_other_seat_starts():
    # first_seat=1, so seat 0 playing the opening move is out of turn.
    with pytest.raises(HTTPException) as exc:
        _authorize(first_seat=1)
    assert exc.value.status_code == 403
    # And seat 1 may open.
    assert _authorize(first_seat=1, token="tok1") == 1


# --- presence ---


def _seat(**over) -> dict:
    base = dict(seat=0, token_hash="h", name="Ada", colour="a", joined=True)
    base.update(over)
    return base


def test_a_seat_that_left_is_not_present():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert seat_present(_seat(joined=False, last_seen=now.isoformat()), now) is False


def test_a_seat_that_has_not_read_yet_is_still_arriving():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert seat_present(_seat(), now) is True


def test_a_seat_goes_quiet_after_the_presence_window():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    window = settings.room_presence_timeout_seconds
    fresh = _seat(last_seen=(now - timedelta(seconds=window - 1)).isoformat())
    stale = _seat(last_seen=(now - timedelta(seconds=window + 1)).isoformat())
    assert seat_present(fresh, now) is True
    assert seat_present(stale, now) is False


def test_an_unreadable_last_seen_does_not_evict_a_seat():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert seat_present(_seat(last_seen="not-a-date"), now) is True


def test_free_seat_finds_the_gap_a_departure_leaves():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    both = [_seat(seat=0), _seat(seat=1)]
    assert free_seat(both, now) is None
    # Seat 0 walked out, so that is the seat on offer.
    assert free_seat([_seat(seat=0, joined=False), _seat(seat=1)], now) == 0
    assert free_seat([_seat(seat=0)], now) == 1


# --- new routes ---


def test_leave_forwards_the_token(monkeypatch):
    captured = {}

    def fake_leave(session, **kw):
        captured.update(kw)
        return _room(status="waiting")

    monkeypatch.setattr(service, "leave_room", fake_leave)
    res = client.post("/rooms/ABCDEF/leave", json={"token": "tok"})
    assert res.status_code == 200
    assert captured == {"code": "ABCDEF", "token": "tok"}


def test_start_forwards_the_token(monkeypatch):
    captured = {}

    def fake_start(session, **kw):
        captured.update(kw)
        return _room(status="active")

    monkeypatch.setattr(service, "start_game", fake_start)
    res = client.post("/rooms/ABCDEF/start", json={"token": "tok"})
    assert res.status_code == 200
    assert captured == {"code": "ABCDEF", "token": "tok"}


def test_matchmake_forwards_the_search(monkeypatch):
    captured = {}
    room = _room(status="waiting")

    def fake_matchmake(session, **kw):
        captured.update(kw)
        return room, "tok"

    monkeypatch.setattr(service, "matchmake", fake_matchmake)
    monkeypatch.setattr(service, "seat_for_token", lambda seats, token: 1)
    res = client.post(
        "/rooms/matchmake",
        json={"gameId": "tic-tac-toe", "name": "Ada", "colour": "1,2,3", "cellCount": 64},
    )
    assert res.status_code == 200
    assert res.json()["seat"] == 1
    assert captured["game_id"] == "tic-tac-toe"
    assert captured["cell_count"] == 64


def test_a_wrong_length_code_is_not_a_room(monkeypatch):
    monkeypatch.setattr(service, "get_room", lambda s, code: _room())
    assert client.get("/rooms/ABCDEF").status_code == 200
    # The server only ever mints CODE_LENGTH codes, so anything else is rejected before a lookup.
    assert client.get("/rooms/ABCDE").status_code == 422
    assert client.get("/rooms/ABCDEFG").status_code == 422


def test_state_reports_the_room_settings_and_outcome(monkeypatch):
    room = _room(
        status="finished",
        moves=[0, 1],
        first_seat=1,
        is_open=True,
        move_limit_seconds=60,
        outcome="timeout",
        winner_seat=0,
    )
    monkeypatch.setattr(service, "get_room", lambda s, code: room)
    body = client.get("/rooms/ABCDEF").json()
    assert body["firstSeat"] == 1
    assert body["isOpen"] is True
    assert body["moveLimitSeconds"] == 60
    assert body["outcome"] == "timeout"
    assert body["winnerSeat"] == 0
    # A finished room has no clock running, so there is nothing to count down to.
    assert body["turnEndsAt"] is None


def test_a_poll_carrying_a_seat_token_marks_that_seat_seen(monkeypatch):
    captured = {}

    def fake_touch(session, **kw):
        captured.update(kw)
        return _room(status="active")

    monkeypatch.setattr(service, "touch_seat", fake_touch)
    res = client.get("/rooms/ABCDEF", headers={"X-Seat-Token": "tok"})
    assert res.status_code == 200
    assert captured == {"code": "ABCDEF", "token": "tok"}


# --- the move clock ---


class _FakeSession:
    """Stands in for a Session where the clock only needs to commit what it decided."""

    def __init__(self):
        self.commits = 0
        self.flushes = 0

    def commit(self):
        self.commits += 1

    def flush(self):
        # The clock and presence checks flush, so the caller's commit stays the only boundary.
        self.flushes += 1

    def refresh(self, _obj):
        pass


def test_a_room_without_a_limit_has_no_deadline():
    room = _room(status="active", turn_started_at=datetime(2030, 1, 1, tzinfo=UTC))
    assert turn_deadline(room) is None


def test_a_deadline_counts_from_when_the_turn_started():
    started = datetime(2030, 1, 1, tzinfo=UTC)
    room = _room(status="active", move_limit_seconds=60, turn_started_at=started)
    assert turn_deadline(room) == started + timedelta(seconds=60)


def test_the_clock_leaves_a_turn_alone_while_time_remains():
    room = _room(
        status="active",
        move_limit_seconds=600,
        turn_started_at=service.now_utc(),
        first_seat=0,
    )
    session = _FakeSession()
    enforce_clock(session, room)
    assert room.status == "active"
    assert room.outcome is None
    assert session.commits == 0


def test_running_out_of_time_hands_the_game_to_the_other_seat():
    # Seat 0 opened and no moves are in, so seat 0 is the one on the clock.
    room = _room(
        status="active",
        moves=[],
        first_seat=0,
        move_limit_seconds=30,
        turn_started_at=service.now_utc() - timedelta(seconds=31),
    )
    enforce_clock(_FakeSession(), room)
    assert room.status == "finished"
    assert room.outcome == "timeout"
    assert room.winner_seat == 1


def test_the_clock_blames_whoever_is_actually_on_turn():
    # One move played, so it is seat 1 who let the clock run out.
    room = _room(
        status="active",
        moves=[5],
        first_seat=0,
        move_limit_seconds=30,
        turn_started_at=service.now_utc() - timedelta(seconds=31),
    )
    enforce_clock(_FakeSession(), room)
    assert room.winner_seat == 0


# --- matchmaking must not offer rooms nobody is sitting in ---


def test_a_waiting_room_whose_host_has_gone_is_not_a_match():
    """
    Regression: the matchmaker offers the oldest open room first, and an abandoned one still reads
    as joinable. Two people searching at once each land in a different empty room instead of finding
    each other.
    """
    window = settings.room_presence_timeout_seconds
    stale = (service.now_utc() - timedelta(seconds=window + 5)).isoformat()
    ghost = _room(
        code="GHOST1",
        is_open=True,
        seats=[_seat(seat=0, name="Gone", last_seen=stale)],
    )
    # Nobody is there, so there is nothing to play against.
    assert service.present_seats(ghost.seats, service.now_utc()) == []
    # The seat does read as free, which is exactly why the free-seat check alone was not enough.
    assert service.free_seat(ghost.seats, service.now_utc()) == 0


def test_a_waiting_room_with_a_live_host_is_a_match():
    host = _room(
        code="LIVE01",
        is_open=True,
        seats=[_seat(seat=0, name="Host", last_seen=service.now_utc().isoformat())],
    )
    now = service.now_utc()
    assert len(service.present_seats(host.seats, now)) == 1
    # The joiner takes the second seat rather than replacing the host.
    assert service.free_seat(host.seats, now) == 1


# --- a waiting room's settings belong to whoever opened it ---


def test_settings_forwards_the_new_terms(monkeypatch):
    captured = {}

    def fake_update(session, **kw):
        captured.update(kw)
        return _room(first_seat=1, is_open=True, move_limit_seconds=60)

    monkeypatch.setattr(service, "update_settings", fake_update)
    res = client.post(
        "/rooms/ABCDEF/settings",
        json={"token": "tok", "firstSeat": 1, "isOpen": True, "moveLimitSeconds": 60},
    )
    assert res.status_code == 200
    assert captured == {
        "code": "ABCDEF",
        "token": "tok",
        "first_seat": 1,
        "is_open": True,
        "move_limit_seconds": 60,
    }
    # The response describes the room, so a client can render what it now is.
    body = res.json()
    assert body["firstSeat"] == 1
    assert body["isOpen"] is True
    assert body["moveLimitSeconds"] == 60


def test_settings_rejects_a_seat_that_does_not_own_the_room():
    seats = _seats_with_tokens("opener", "joiner")
    room = _room(seats=seats, status="waiting")
    session = _FakeSession()

    def fake_live(_session, _code, lock=False):
        return room

    original = service._live_room
    service._live_room = fake_live  # type: ignore[assignment]
    try:
        with pytest.raises(HTTPException) as exc:
            service.update_settings(
                session,
                code="ABCDEF",
                token="joiner",
                first_seat=1,
                is_open=True,
                move_limit_seconds=None,
            )
        assert exc.value.status_code == 403
    finally:
        service._live_room = original  # type: ignore[assignment]


def test_settings_are_settled_once_the_game_starts():
    seats = _seats_with_tokens("opener", "joiner")
    room = _room(seats=seats, status="active")
    original = service._live_room
    service._live_room = lambda _s, _c, lock=False: room  # type: ignore[assignment]
    try:
        with pytest.raises(HTTPException) as exc:
            service.update_settings(
                _FakeSession(),
                code="ABCDEF",
                token="opener",
                first_seat=1,
                is_open=False,
                move_limit_seconds=None,
            )
        assert exc.value.status_code == 409
    finally:
        service._live_room = original  # type: ignore[assignment]


# --- nothing starts on its own ---


def _room_with_both(**over) -> Room:
    """A room both players are sitting in, fresh enough to count as present."""
    now = service.now_utc().isoformat()
    seats = [{**entry, "last_seen": now} for entry in _seats_with_tokens("opener", "joiner")]
    return _room(seats=seats, **over)


def _patch_live(room: Room):
    """Points the room lookup at one object, so a start can be driven without a database."""
    original = service._live_room
    service._live_room = lambda _s, _c, lock=False: room  # type: ignore[assignment]
    return original


def test_filling_the_second_seat_does_not_start_the_game():
    room = _room(status="waiting", seats=_seats_with_tokens("opener", "joiner")[:1])
    session = _FakeSession()
    original = _patch_live(room)
    try:
        service._claim_seat(session, room, name="Bo", colour="4,5,6")
    finally:
        service._live_room = original  # type: ignore[assignment]
    # Ready, not running: somebody still has to press start, which is what keeps the settings open.
    assert room.status == "waiting"
    assert room.turn_started_at is None


def test_starting_clears_the_last_game_and_begins_play():
    room = _room_with_both(status="finished", moves=[1, 2, 3], outcome="win", winner_seat=0)
    original = _patch_live(room)
    try:
        service.start_game(_FakeSession(), code="ABCDEF", token="opener")
    finally:
        service._live_room = original  # type: ignore[assignment]
    assert room.status == "active"
    assert room.moves == []
    assert room.outcome is None
    assert room.winner_seat is None
    assert room.turn_started_at is not None


def test_only_the_owner_may_start():
    room = _room_with_both(status="waiting")
    original = _patch_live(room)
    try:
        with pytest.raises(HTTPException) as exc:
            service.start_game(_FakeSession(), code="ABCDEF", token="joiner")
        assert exc.value.status_code == 403
        service.start_game(_FakeSession(), code="ABCDEF", token="opener")
    finally:
        service._live_room = original  # type: ignore[assignment]
    assert room.status == "active"


def test_the_room_passes_to_whoever_is_left_when_the_owner_walks_out():
    room = _room_with_both(status="waiting")
    original = _patch_live(room)
    try:
        service.leave_room(_FakeSession(), code="ABCDEF", token="opener")
        assert room.owner_seat == 1
        # The seat that stayed can now set the terms, and a stranger filling seat 0 cannot.
        service.update_settings(
            _FakeSession(),
            code="ABCDEF",
            token="joiner",
            first_seat=1,
            is_open=True,
            move_limit_seconds=None,
        )
    finally:
        service._live_room = original  # type: ignore[assignment]
    assert room.first_seat == 1


def test_the_owner_keeps_the_room_when_the_other_player_leaves():
    room = _room_with_both(status="waiting")
    original = _patch_live(room)
    try:
        service.leave_room(_FakeSession(), code="ABCDEF", token="joiner")
    finally:
        service._live_room = original  # type: ignore[assignment]
    assert room.owner_seat == 0


def test_starting_needs_both_players_here():
    room = _room(status="waiting", seats=_seats_with_tokens("opener", "joiner")[:1])
    original = _patch_live(room)
    try:
        with pytest.raises(HTTPException) as exc:
            service.start_game(_FakeSession(), code="ABCDEF", token="opener")
        assert exc.value.status_code == 409
    finally:
        service._live_room = original  # type: ignore[assignment]


def test_starting_a_running_game_is_refused():
    room = _room_with_both(status="active")
    original = _patch_live(room)
    try:
        with pytest.raises(HTTPException) as exc:
            service.start_game(_FakeSession(), code="ABCDEF", token="opener")
        assert exc.value.status_code == 409
    finally:
        service._live_room = original  # type: ignore[assignment]


# --- the model and the migrations spell the same SQL ---


def _migration(revision: str):
    """Loads a migration by revision id; alembic/versions is not an importable package."""
    root = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    path = next(p for p in root.glob("*.py") if p.name.startswith(revision))
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_constraints() -> dict[str, str]:
    return {
        arg.name: str(arg.sqltext)
        for arg in Room.__table_args__
        if isinstance(arg, CheckConstraint) and arg.name
    }


def test_the_status_and_outcome_checks_match_the_enums_they_come_from():
    # The enums are the source; a member added or renamed without a migration shows up here.
    assert constants.ROOM_STATUS_CHECK == "status IN ('waiting','active','finished','abandoned')"
    assert (
        constants.ROOM_OUTCOME_CHECK
        == "outcome IS NULL OR outcome IN ('win','draw','timeout','forfeit')"
    )
    checks = _check_constraints()
    assert checks["ck_rooms_status"] == constants.ROOM_STATUS_CHECK
    assert checks["ck_rooms_outcome"] == constants.ROOM_OUTCOME_CHECK
    # The same text the table was created with, so the live constraint still admits every member.
    created = _migration("a0b1c2d3e4f5")
    assert constants.ROOM_STATUS_CHECK in inspect.getsource(created.upgrade)
    outcomes = _migration("c5d6e7f8a9b0")
    assert constants.ROOM_OUTCOME_CHECK in inspect.getsource(outcomes.upgrade)


def test_the_winner_seat_check_and_open_index_match_their_migration():
    migration = _migration("e7f8a9b0c1d2")
    assert _check_constraints()["ck_rooms_winner_seat"] == constants.ROOM_WINNER_SEAT_CHECK
    assert migration._WINNER_SEAT_CHECK == constants.ROOM_WINNER_SEAT_CHECK
    assert migration._OPEN_INDEX_WHERE == constants.ROOM_OPEN_INDEX_WHERE
    assert tuple(migration._OPEN_INDEX_COLUMNS) == constants.ROOM_OPEN_INDEX_COLUMNS
    index = next(i for i in Room.__table__.indexes if i.name == "ix_rooms_open")
    assert tuple(c.name for c in index.columns) == constants.ROOM_OPEN_INDEX_COLUMNS
    assert str(index.dialect_options["postgresql"]["where"]) == constants.ROOM_OPEN_INDEX_WHERE
    # Matchmaking reads exactly the statuses the index admits.
    assert constants.MATCHABLE_STATUSES == (constants.RoomStatus.waiting, RoomStatus.finished)


def test_the_owner_may_change_settings_after_a_game_as_well_as_before():
    for state in ("waiting", "finished"):
        room = _room_with_both(status=state)
        original = _patch_live(room)
        try:
            service.update_settings(
                _FakeSession(),
                code="ABCDEF",
                token="opener",
                first_seat=1,
                is_open=True,
                move_limit_seconds=60,
            )
        finally:
            service._live_room = original  # type: ignore[assignment]
        assert room.first_seat == 1
        assert room.is_open is True
        assert room.move_limit_seconds == 60
