import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.modules.rooms import service
from app.modules.rooms.models import Room
from app.modules.rooms.service import authorize_move, random_code, seat_for_token
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
        seats=[{"seat": 0, "token_hash": "h0", "colour": "1,2,3", "joined": True}],
        status="waiting",
    )
    base.update(over)
    return Room(**base)


# --- routes (service stubbed, no DB) ---


def test_create_returns_seat_zero_and_token(monkeypatch):
    room = _room()
    monkeypatch.setattr(service, "create_room", lambda s, **kw: (room, "secret-token"))
    res = client.post("/rooms", json={"gameId": "tic-tac-toe", "colour": "1,2,3", "cellCount": 64})
    assert res.status_code == 200
    body = res.json()
    assert body["seat"] == 0
    assert body["token"] == "secret-token"
    assert body["gameId"] == "tic-tac-toe"  # camelCase on the wire


def test_create_rejects_out_of_range_cell_count():
    res = client.post("/rooms", json={"gameId": "x", "colour": "c", "cellCount": 99999})
    assert res.status_code == 422


def test_state_never_exposes_seat_tokens(monkeypatch):
    from datetime import UTC, datetime

    room = _room(
        moves=[0, 1],
        status="active",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        seats=[
            {"seat": 0, "token_hash": "h0", "colour": "1,2,3", "joined": True},
            {"seat": 1, "token_hash": "h1", "colour": "4,5,6", "joined": True},
        ],
    )
    monkeypatch.setattr(service, "get_room", lambda s, code: room)
    res = client.get("/rooms/ABCDEF")
    assert res.status_code == 200
    body = res.json()
    assert body["version"] == 2
    assert body["moves"] == [0, 1]
    # The wire form of a seat carries no token, hashed or otherwise.
    assert set(body["seats"][0]) == {"seat", "colour", "joined"}
    assert "tokenHash" not in body["seats"][0]


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
    assert res.json() == {"version": 1, "moves": [5], "status": "active"}
    assert captured["move"] == 5
    # camelCase expectedVersion on the wire arrives as snake_case in the service call.
    assert captured["expected_version"] == 0


def test_move_rejects_pass_sentinel_below_range():
    # -1 is the reserved pass value (max is fine for the schema), but -2 is out of the schema bound.
    res = client.post("/rooms/ABCDEF/move", json={"token": "t", "move": -2, "expectedVersion": 0})
    assert res.status_code == 422


# --- authorize_move: pure enforcement, no DB ---


def _seats_with_tokens(t0: str, t1: str) -> list[dict]:
    import hashlib

    def h(t):
        return hashlib.sha256(t.encode()).hexdigest()

    return [
        {"seat": 0, "token_hash": h(t0), "colour": "a", "joined": True},
        {"seat": 1, "token_hash": h(t1), "colour": "b", "joined": True},
    ]


def _authorize(**over):
    seats = over.pop("seats", _seats_with_tokens("tok0", "tok1"))
    base = dict(
        moves=[],
        seats=seats,
        cell_count=64,
        game_id="tic-tac-toe",
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
    with pytest.raises(InvalidMove):
        default_placement_check([0, 1], -1, 64)  # pass is not legal for a placement game
    with pytest.raises(InvalidMove):
        default_placement_check([0, 1], 1, 64)  # occupied
    default_placement_check([0, 1], 2, 64)  # a free in-range cell is fine


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
        assert len(code) == 6
        # No visually ambiguous characters.
        assert set(code) <= set("ABCDEFGHJKMNPQRSTUVWXYZ23456789")


def test_seat_for_token_constant_lookup():
    seats = _seats_with_tokens("aaa", "bbb")
    assert seat_for_token(seats, "aaa") == 0
    assert seat_for_token(seats, "bbb") == 1
    with pytest.raises(HTTPException) as exc:
        seat_for_token(seats, "ccc")
    assert exc.value.status_code == 403
