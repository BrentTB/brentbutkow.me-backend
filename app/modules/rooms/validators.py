"""Per-game rules — the one place a game's own logic lives on the server.

The rooms transport is game-agnostic: it enforces whose turn it is, that the client is on the
right version, and that the caller holds the seat. It knows nothing about what makes a move legal,
or about what wins. Both are delegated here, keyed by ``game_id``.

Two hooks, each with a registry:

* ``GAME_VALIDATORS`` — legality. The fallback is ``default_placement_check``: a move must land on
  an in-range, still-empty cell. That is a complete rule for placement games, so 4x4x4 tic-tac-toe
  registers nothing. A game whose legality can't be expressed structurally (Othello, where a move
  must flip at least one disc, and a pass is legal only when nothing else is) registers a validator
  that takes full responsibility for the move, including its own range and pass handling.
* ``GAME_OUTCOMES`` — who won. A game that registers a judge has its result decided by the server;
  the mover's claim is ignored. A game with no judge is trusted, which is a trust boundary the
  service documents.
"""

from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple, Protocol

from app.modules.rooms.constants import MAX_SEATS


class InvalidMove(Exception):
    """Raised by a validator when a move is illegal; the router turns it into a 422."""


class MoveValidator(Protocol):
    def __call__(self, moves: list[int], move: int, seat: int, cell_count: int) -> None:
        """Return normally if the move is legal, else raise ``InvalidMove``."""


def default_placement_check(moves: list[int], move: int, seat: int, cell_count: int) -> None:
    """A move must be an in-range cell that has not been played yet.

    Takes ``seat`` to match ``MoveValidator``, and ignores it: for a placement game a legal move is
    exactly "a free cell", whoever is playing it.
    """
    if not 0 <= move < cell_count:
        raise InvalidMove("cell out of range")
    if move in moves:
        raise InvalidMove("cell already taken")


# Games register here only when the default placement rule is not enough. Empty today; Othello adds
# an entry when it ships. Adding a game touches this map, never the router or service.
GAME_VALIDATORS: dict[str, MoveValidator] = {}


def validate_move(game_id: str, moves: list[int], move: int, seat: int, cell_count: int) -> None:
    """Run the game's validator, or the default placement check if it has none."""
    validator = GAME_VALIDATORS.get(game_id, default_placement_check)
    validator(moves, move, seat, cell_count)


class Verdict(NamedTuple):
    """Whether the game is over, and who took it. ``winner_seat`` is None for a draw."""

    finished: bool
    winner_seat: int | None


class OutcomeJudge(Protocol):
    def __call__(self, moves: list[int], first_seat: int, cell_count: int) -> Verdict | None:
        """The result the move list implies, or None when this judge cannot read the board."""


def _cube_edge(cell_count: int) -> int | None:
    edge = round(cell_count ** (1 / 3))
    return edge if edge >= 2 and edge**3 == cell_count else None


def _step_directions() -> list[tuple[int, int, int]]:
    """The 13 directions a line can run through a cube.

    Every direction has an opposite tracing the same cells, so only the one whose first non-zero
    step is positive is kept.
    """
    directions: list[tuple[int, int, int]] = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                lead = next((step for step in (dx, dy, dz) if step != 0), None)
                if lead is None or lead < 0:
                    continue
                directions.append((dx, dy, dz))
    return directions


@lru_cache(maxsize=8)
def cube_win_lines(cell_count: int) -> tuple[tuple[int, ...], ...]:
    """Every straight run the length of one edge in an edge x edge x edge board.

    Rows, columns, rods, the diagonals inside each plane, and the body diagonals — counted once
    each, from the end where stepping backwards would leave the cube. Cells are numbered
    ``x + edge*y + edge*edge*z``; because a cube's set of lines is the same however the axes are
    labelled, this matches any client using the same flat numbering. Empty when the cell count is
    not a cube.
    """
    edge = _cube_edge(cell_count)
    if edge is None:
        return ()

    def index(x: int, y: int, z: int) -> int:
        return z * edge * edge + y * edge + x

    def inside(value: int) -> bool:
        return 0 <= value < edge

    lines: list[tuple[int, ...]] = []
    last = edge - 1
    for z in range(edge):
        for y in range(edge):
            for x in range(edge):
                for dx, dy, dz in _step_directions():
                    if inside(x - dx) and inside(y - dy) and inside(z - dz):
                        continue  # the same line, counted from an earlier cell
                    if not (
                        inside(x + last * dx) and inside(y + last * dy) and inside(z + last * dz)
                    ):
                        continue
                    lines.append(
                        tuple(
                            index(x + step * dx, y + step * dy, z + step * dz)
                            for step in range(edge)
                        )
                    )
    return tuple(lines)


def cube_placement_outcome(moves: list[int], first_seat: int, cell_count: int) -> Verdict | None:
    """The result of a cubic placement game: a full line wins, a full board draws.

    Reads only what the room already stores — the move list and who opened — so the server never
    has to take the mover's word for a win.
    """
    lines = cube_win_lines(cell_count)
    if not lines:
        return None
    owner = {cell: (first_seat + played) % MAX_SEATS for played, cell in enumerate(moves)}
    for line in lines:
        holders = {owner.get(cell) for cell in line}
        if len(holders) == 1 and None not in holders:
            return Verdict(True, holders.pop())
    if len(moves) >= cell_count:
        return Verdict(True, None)
    return Verdict(False, None)


# Games whose result the server works out for itself. A game absent from this map keeps the client's
# word for it — see ``append_move``.
GAME_OUTCOMES: dict[str, OutcomeJudge] = {"tic-tac-toe": cube_placement_outcome}


def judge_outcome(
    game_id: str, *, moves: list[int], first_seat: int, cell_count: int
) -> Verdict | None:
    """The server's own reading of the position, or None when this game has no judge for it."""
    judge = GAME_OUTCOMES.get(game_id)
    if judge is None:
        return None
    return judge(moves, first_seat, cell_count)
