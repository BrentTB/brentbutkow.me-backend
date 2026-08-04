"""Scaffolding the rooms test modules share: a room builder, a pinned clock, a Session stand-in.

Not a test module. Three files need the same room fixture and the same control over the clock —
tests/test_rooms.py (routes and pure logic), tests/test_rooms_service.py (service branches) and
tests/test_rooms_concurrency.py (Postgres) — so it lives here once.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from app.modules.rooms import constants, service
from app.modules.rooms.constants import RoomStatus
from app.modules.rooms.models import Room

GAME_ID = "tic-tac-toe"
# 4x4x4, the board the site plays on: a real cube, so the outcome judge reads it.
CELL_COUNT = 64
# Deliberately before ROOM_EXPIRY, so a room built here is still live under the pinned clock.
NOW = datetime(2029, 12, 31, 12, 0, tzinfo=UTC)
ROOM_EXPIRY = datetime(2030, 1, 1, tzinfo=UTC)


class Clock:
    """A clock a test drives by hand, so presence and timeouts are arithmetic instead of sleeps."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


def pin_clock(monkeypatch, start: datetime = NOW) -> Clock:
    """Pins the one clock the rooms code reads — the service's and the constants' own reference."""
    clock = Clock(start)
    monkeypatch.setattr(service, "now_utc", clock)
    monkeypatch.setattr(constants, "now_utc", clock)
    return clock


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def seat(index: int = 0, token: str | None = None, **over) -> dict:
    """One seats-array record. Given a token, the stored hash really matches it."""
    entry = dict(
        seat=index,
        token_hash=token_hash(token) if token is not None else f"h{index}",
        name="Ada",
        colour="1,2,3",
        joined=True,
    )
    entry.update(over)
    return entry


def seats_with_tokens(first: str, second: str, *, last_seen: datetime | None = None) -> list[dict]:
    """Both seats, each holding its own token. With ``last_seen`` set, both read as present."""
    stamp = {"last_seen": last_seen.isoformat()} if last_seen is not None else {}
    return [
        seat(0, first, name="Ada", colour="1,2,3", **stamp),
        seat(1, second, name="Bo", colour="4,5,6", **stamp),
    ]


def room(**over) -> Room:
    base = dict(
        code="ABCDEF",
        game_id=GAME_ID,
        cell_count=CELL_COUNT,
        moves=[],
        seats=[seat(0)],
        status=RoomStatus.waiting,
        expires_at=ROOM_EXPIRY,
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


class FakeScalars:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class FakeSession:
    """A Session stand-in: counts the transaction boundaries and answers reads from a queue.

    ``rows`` answers ``scalar()`` in order and then keeps returning None, which is what a free room
    code and a code nobody has look like; ``candidates`` is what ``scalars().all()`` hands back.
    Every statement is recorded, so a test can assert what kind of write a branch issued.
    """

    def __init__(self, *, rows=None, candidates=None) -> None:
        self.commits = 0
        self.flushes = 0
        self.added: list = []
        self.executed: list = []
        self._rows = list(rows or [])
        self._candidates = list(candidates or [])

    def commit(self):
        self.commits += 1

    def flush(self):
        # The clock and presence checks flush, so the caller's commit stays the only boundary.
        self.flushes += 1

    def refresh(self, _obj):
        pass

    def rollback(self):
        pass

    def add(self, obj):
        self.added.append(obj)

    def execute(self, statement):
        self.executed.append(statement)
        return None

    def scalar(self, statement):
        self.executed.append(statement)
        return self._rows.pop(0) if self._rows else None

    def scalars(self, statement):
        self.executed.append(statement)
        return FakeScalars(self._candidates)
