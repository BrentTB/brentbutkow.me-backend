"""The rooms vocabulary: statuses, outcomes, the seat record, and the SQL those spell out.

Deliberately import-light — no SQLAlchemy, no FastAPI — so the model, the service and the schemas
can all import it without a cycle. The CHECK/index expressions live here because a model and a
migration both spell them, and a shared source plus a sync test is what keeps them from drifting.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import NamedTuple, NotRequired, TypedDict


class GameId(StrEnum):
    """The games the rooms transport knows by name.

    A ``game_id`` on the wire is still a free string — an unknown one is a game with no special
    rules, which is deliberately allowed. These members are the keys the per-game registries
    (validators, outcome judges, board sizes) are spelled with, so a game opts into server-side
    logic in one place and a typo is a ``NameError`` rather than a silent fall-through to defaults.
    """

    tic_tac_toe = "tic-tac-toe"
    othello = "othello"


class RoomStatus(StrEnum):
    """Where a room is in its life. Member order is the order the CHECK constraint lists."""

    # Nobody is playing: either the room's first game has not started, or its last one was cleared.
    waiting = "waiting"
    # A game is running, so the board and the settings are settled.
    active = "active"
    # A game ended and its result is still readable, until someone starts the next one.
    finished = "finished"
    # Matchmaking gave up on it: everybody left without pressing leave, so it stops being offered.
    abandoned = "abandoned"


class RoomOutcome(StrEnum):
    """How a game ended. Member order is the order the CHECK constraint lists."""

    win = "win"
    draw = "draw"
    timeout = "timeout"
    forfeit = "forfeit"


# The two statuses matchmaking may hand to a stranger: a room waiting for its first game, and one
# whose last game is over — the board clears as the new player sits down.
MATCHABLE_STATUSES = (RoomStatus.waiting, RoomStatus.finished)

MAX_SEATS = 2


class Verdict(NamedTuple):
    """Whether a game is over, and who took it. ``winner_seat`` is None for a draw.

    Lives here, not in ``validators``, so ``othello`` can return one without importing back into the
    module that imports it.
    """

    finished: bool
    winner_seat: int | None


class SeatEntry(TypedDict):
    """One record in the ``rooms.seats`` JSONB array.

    ``token_hash`` is the sha256 of the seat's token; the raw token is returned once and never
    stored. ``last_seen`` is an ISO timestamp of that seat's last read and is missing until the
    player polls for the first time. ``pending_move`` is a move this seat has aimed but not yet
    committed; if the clock runs out on their turn while it is set, the server plays it for them
    instead of forfeiting. Cleared the moment any move is appended.
    """

    seat: int
    token_hash: str
    name: str
    colour: str
    joined: bool
    last_seen: NotRequired[str]
    pending_move: NotRequired[int]


def now_utc() -> datetime:
    """The one clock the rooms code reads, so a test can pin time in a single place."""
    return datetime.now(UTC)


def _sql_values(values: Iterable[StrEnum]) -> str:
    return ",".join(f"'{value.value}'" for value in values)


ROOM_STATUS_CHECK = f"status IN ({_sql_values(RoomStatus)})"
ROOM_OUTCOME_CHECK = f"outcome IS NULL OR outcome IN ({_sql_values(RoomOutcome)})"
ROOM_FIRST_SEAT_CHECK = "first_seat IN (0,1)"
ROOM_OWNER_SEAT_CHECK = "owner_seat IN (0,1)"
ROOM_WINNER_SEAT_CHECK = "winner_seat IS NULL OR winner_seat IN (0,1)"
# Matchmaking reads the oldest matchable open room. The size-specific pool seeks on game_id +
# cell_count and reads in created_at order; the any-size pool (MATCH_ANY_SIZE_GAMES) drops the
# cell_count predicate, so it needs its own (game_id, created_at) index to stay an ordered seek
# rather than a sort of the whole open-room backlog. Both share this WHERE.
ROOM_OPEN_INDEX_WHERE = f"is_open AND status IN ({_sql_values(MATCHABLE_STATUSES)})"
ROOM_OPEN_INDEX_COLUMNS = ("game_id", "cell_count", "created_at")
ROOM_OPEN_ANY_SIZE_INDEX_COLUMNS = ("game_id", "created_at")
