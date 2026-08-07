"""Othello rules on the server, so the room does not have to take the client's word for a move.

The room stores only the move list, so both the legality check and the outcome judge rebuild the
board by replaying it. Dark always opens, so the colour of the move at position *i* is dark when *i*
is even and light when it is odd — no matter which seat opened. That is what lets the validator work
from the move list alone, without knowing ``first_seat``; only the judge needs it, to name the
winning *seat* rather than the winning colour.

**Cross-repo invariant:** the seat↔colour mapping here (the opener — ``first_seat`` — is dark) must
match ``src/projects/Othello/online.ts`` in the frontend. Change one, change the other.
"""

from __future__ import annotations

from math import isqrt

from app.modules.rooms.constants import Verdict

# Board cells hold one of these. Dark opens, by Othello convention.
DARK = 1
LIGHT = 2
EMPTY = 0

# The reserved move value for "I have no legal move and forfeit my turn".
PASS = -1

# The eight directions a capture line can run.
_DIRECTIONS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _opponent(colour: int) -> int:
    return LIGHT if colour == DARK else DARK


def board_size(cell_count: int) -> int | None:
    """The edge length for a square board of ``cell_count`` cells, or None when it is not square."""
    edge = isqrt(cell_count)
    return edge if edge >= 2 and edge * edge == cell_count else None


def _captures_at(board: list[int], index: int, colour: int, size: int) -> list[int]:
    """The discs a move at ``index`` would flip for ``colour``. Empty when the move is illegal."""
    if not 0 <= index < len(board) or board[index] != EMPTY:
        return []
    row, col = divmod(index, size)
    them = _opponent(colour)
    flipped: list[int] = []
    for dr, dc in _DIRECTIONS:
        line: list[int] = []
        r, c = row + dr, col + dc
        while 0 <= r < size and 0 <= c < size and board[r * size + c] == them:
            line.append(r * size + c)
            r += dr
            c += dc
        # A run of the opponent's discs only counts once it is closed by one of ours.
        if line and 0 <= r < size and 0 <= c < size and board[r * size + c] == colour:
            flipped.extend(line)
    return flipped


def _has_legal_move(board: list[int], colour: int, size: int) -> bool:
    # _captures_at already returns [] for a non-empty or out-of-range cell, so it is the whole test.
    return any(_captures_at(board, index, colour, size) for index in range(len(board)))


def _starting_board(size: int) -> list[int]:
    board = [EMPTY] * (size * size)
    mid = size // 2
    board[(mid - 1) * size + (mid - 1)] = LIGHT
    board[(mid - 1) * size + mid] = DARK
    board[mid * size + (mid - 1)] = DARK
    board[mid * size + mid] = LIGHT
    return board


def _colour_at_turn(played: int) -> int:
    """The colour to move after ``played`` moves. Dark opens, so even plies are dark."""
    return DARK if played % 2 == 0 else LIGHT


def replay(moves: list[int], size: int) -> list[int]:
    """The board after ``moves`` are applied in order, passes (``-1``) included as spent turns."""
    board = _starting_board(size)
    for played, move in enumerate(moves):
        colour = _colour_at_turn(played)
        if move == PASS:
            continue
        # A stored move outside the board would only exist through corruption; skip it rather than
        # let the write below raise IndexError and turn a bad row into a 500.
        if not 0 <= move < len(board):
            continue
        for cell in _captures_at(board, move, colour, size):
            board[cell] = colour
        board[move] = colour
    return board


def is_legal(moves: list[int], move: int, cell_count: int) -> tuple[bool, str]:
    """Whether ``move`` is legal for the side to move. Returns the reason when it is not."""
    size = board_size(cell_count)
    if size is None:
        return False, "board is not square"

    board = replay(moves, size)
    colour = _colour_at_turn(len(moves))

    if move == PASS:
        # Forfeiting a turn is legal only when there is genuinely nothing else to do.
        if _has_legal_move(board, colour, size):
            return False, "a legal move is available, cannot pass"
        return True, ""

    if not 0 <= move < cell_count:
        return False, "cell out of range"
    if board[move] != EMPTY:
        return False, "cell already taken"
    if not _captures_at(board, move, colour, size):
        return False, "move must flip at least one disc"
    return True, ""


def outcome(moves: list[int], first_seat: int, cell_count: int) -> Verdict | None:
    """The verdict for the position, or None when the board is not square.

    The game is over when neither colour can move. The winner is whoever holds more discs; equal
    counts are a draw (winner None). ``winner_seat`` maps the winning colour back to a seat: the
    opener, ``first_seat``, is dark.
    """
    size = board_size(cell_count)
    if size is None:
        return None

    board = replay(moves, size)
    if _has_legal_move(board, DARK, size) or _has_legal_move(board, LIGHT, size):
        return Verdict(False, None)

    dark = board.count(DARK)
    light = board.count(LIGHT)
    if dark == light:
        return Verdict(True, None)
    winning_colour = DARK if dark > light else LIGHT
    winner_seat = first_seat if winning_colour == DARK else 1 - first_seat
    return Verdict(True, winner_seat)
