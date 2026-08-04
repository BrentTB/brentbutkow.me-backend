"""Per-game move legality — the one place a game's rules live on the server.

The rooms transport is game-agnostic: it enforces whose turn it is, that the client is on the
right version, and that the caller holds the seat. It knows nothing about what makes a move legal.
That is delegated here, keyed by ``game_id``.

A game with no registered validator falls back to ``default_placement_check``: a move must land on
an in-range, still-empty cell. That is a complete rule for placement games like 4x4x4 tic-tac-toe
(a legal move is exactly "a free cell on your turn"), so tic-tac-toe registers nothing. A game
whose legality can't be expressed structurally (Othello, where a move must flip at least one disc,
and a pass is legal only when nothing else is) registers a validator that takes full responsibility
for the move, including its own range and pass handling.
"""

from __future__ import annotations

from typing import Protocol


class InvalidMove(Exception):
    """Raised by a validator when a move is illegal; the router turns it into a 422."""


class MoveValidator(Protocol):
    def __call__(self, moves: list[int], move: int, seat: int, cell_count: int) -> None:
        """Return normally if the move is legal, else raise ``InvalidMove``."""


def default_placement_check(moves: list[int], move: int, cell_count: int) -> None:
    """A move must be an in-range cell that has not been played yet."""
    if not 0 <= move < cell_count:
        raise InvalidMove("cell out of range")
    if move in moves:
        raise InvalidMove("cell already taken")


# Games register here only when the default placement rule is not enough. Empty today; Othello adds
# an entry when it ships. Adding a game touches this map, never the router or service.
GAME_VALIDATORS: dict[str, MoveValidator] = {}


def validate_move(game_id: str, moves: list[int], move: int, seat: int, cell_count: int) -> None:
    """Run the game's validator, or the default placement check if it has none."""
    validator = GAME_VALIDATORS.get(game_id)
    if validator is None:
        default_placement_check(moves, move, cell_count)
        return
    validator(moves, move, seat, cell_count)
