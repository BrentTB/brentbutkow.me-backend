"""Othello legality and outcome, and the registry wiring that puts them in front of a room."""

import pytest

from app.modules.rooms import othello
from app.modules.rooms.validators import (
    InvalidMove,
    is_valid_cell_count,
    judge_outcome,
    validate_move,
)

# The standard board the site plays online.
CELLS = 64
SIZE = 8


def _idx(row: int, col: int) -> int:
    return row * SIZE + col


# --- board geometry ---


def test_board_size_accepts_squares_and_rejects_the_rest():
    assert othello.board_size(64) == 8
    assert othello.board_size(36) == 6
    assert othello.board_size(100) == 10
    assert othello.board_size(63) is None
    assert othello.board_size(1) is None


def test_replay_opens_with_the_four_centre_discs():
    board = othello.replay([], SIZE)
    assert board[_idx(3, 3)] == othello.LIGHT
    assert board[_idx(3, 4)] == othello.DARK
    assert board[_idx(4, 3)] == othello.DARK
    assert board[_idx(4, 4)] == othello.LIGHT
    assert board.count(othello.EMPTY) == CELLS - 4


# --- legality ---


def test_opening_move_that_flips_a_disc_is_legal():
    legal, _ = othello.is_legal([], _idx(2, 3), CELLS)
    assert legal


def test_a_move_that_flips_nothing_is_illegal():
    legal, reason = othello.is_legal([], _idx(0, 0), CELLS)
    assert not legal
    assert "flip" in reason


def test_an_occupied_cell_is_illegal():
    legal, reason = othello.is_legal([], _idx(3, 3), CELLS)
    assert not legal
    assert "taken" in reason


def test_an_out_of_range_cell_is_illegal():
    legal, reason = othello.is_legal([], CELLS, CELLS)
    assert not legal
    assert "range" in reason


def test_passing_is_illegal_while_a_move_exists():
    legal, reason = othello.is_legal([], othello.PASS, CELLS)
    assert not legal
    assert "pass" in reason


def test_a_non_square_board_is_never_legal():
    legal, reason = othello.is_legal([], 0, 63)
    assert not legal
    assert "square" in reason


# --- a full game, driven only through the public surface ---


def _play_to_the_end(first_seat: int) -> list[int]:
    """Play first-legal-move for both colours, passing when stuck, until the judge calls it over."""
    moves: list[int] = []
    for _ in range(CELLS * 2):
        verdict = othello.outcome(moves, first_seat, CELLS)
        assert verdict is not None
        if verdict.finished:
            return moves
        played = next(
            (cell for cell in range(CELLS) if othello.is_legal(moves, cell, CELLS)[0]), None
        )
        if played is None:
            legal, _ = othello.is_legal(moves, othello.PASS, CELLS)
            assert legal, "a stuck side must be allowed to pass"
            moves.append(othello.PASS)
        else:
            moves.append(played)
    raise AssertionError("game did not terminate")


def test_a_full_game_ends_and_the_winner_matches_the_disc_count():
    moves = _play_to_the_end(first_seat=0)
    board = othello.replay(moves, SIZE)
    dark, light = board.count(othello.DARK), board.count(othello.LIGHT)
    expected_seat = None if dark == light else (0 if dark > light else 1)
    assert othello.outcome(moves, 0, CELLS) == (True, expected_seat)


def test_the_winning_seat_follows_first_seat():
    # The move list is the same whoever opened; the seat the win maps to flips with first_seat,
    # because the opener is dark.
    moves = _play_to_the_end(first_seat=0)
    zero = othello.outcome(moves, 0, CELLS)
    one = othello.outcome(moves, 1, CELLS)
    assert zero is not None and one is not None
    if zero.winner_seat is None:
        assert one.winner_seat is None  # a draw is a draw whoever opened
    else:
        assert one.winner_seat == 1 - zero.winner_seat


def test_a_game_in_progress_is_not_over():
    assert othello.outcome([], 0, CELLS) == (False, None)


def test_a_fixed_opening_flips_exactly_the_expected_cells():
    # Dark opens at (2,3): the light disc at (3,3) is flanked by dark's new disc above it and dark's
    # own (4,3) below, so it flips — and nothing else changes. Every cell here is hand-computed, so
    # this catches a wrong flip that a replay-derived expectation would move in lockstep with.
    move = _idx(2, 3)
    board = othello.replay([move], SIZE)
    assert board[_idx(2, 3)] == othello.DARK  # the placed disc
    assert board[_idx(3, 3)] == othello.DARK  # flipped from light
    assert board[_idx(3, 4)] == othello.DARK  # untouched centre dark
    assert board[_idx(4, 3)] == othello.DARK  # the flanking dark
    assert board[_idx(4, 4)] == othello.LIGHT  # untouched centre light
    start = othello._starting_board(SIZE)
    changed = [i for i in range(CELLS) if board[i] != start[i]]
    assert sorted(changed) == sorted([_idx(2, 3), _idx(3, 3)])


def test_a_capture_never_wraps_a_row_boundary():
    # A run of the opponent at the right edge of row 0, "closed" only by our disc at the left edge
    # of row 1 — adjacent in the flat array but not on the board. A wrap bug would count it; the
    # row/col walk must not.
    size = 4
    board = [othello.EMPTY] * (size * size)
    board[0 * size + 3] = othello.LIGHT  # (0,3) opponent, right edge
    board[1 * size + 0] = othello.DARK  # (1,0) ours, next flat index but a different row
    assert othello._captures_at(board, 0 * size + 2, othello.DARK, size) == []


# --- registry wiring ---


def test_the_room_uses_the_othello_validator():
    # A flipping move is accepted; a corner that flanks nothing is a 422-worthy InvalidMove.
    validate_move("othello", [], _idx(2, 3), 0, CELLS)
    with pytest.raises(InvalidMove):
        validate_move("othello", [], _idx(0, 0), 0, CELLS)


def test_the_room_uses_the_othello_judge():
    verdict = judge_outcome("othello", moves=[], first_seat=0, cell_count=CELLS)
    assert verdict is not None
    assert verdict.finished is False


def test_the_board_registry_accepts_only_playable_sizes():
    # Othello: an even edge of at least 4. A judge that cannot read the board would otherwise hand
    # the win back to the client's claim, so an unplayable size has to be refused up front.
    assert is_valid_cell_count("othello", 64)  # 8x8
    assert is_valid_cell_count("othello", 36)  # 6x6
    assert not is_valid_cell_count("othello", 63)  # not square
    assert not is_valid_cell_count("othello", 4)  # 2x2 starts full
    assert not is_valid_cell_count("othello", 25)  # 5x5, odd edge
    # tic-tac-toe: a perfect cube, the only board its line judge can read.
    assert is_valid_cell_count("tic-tac-toe", 64)  # 4x4x4
    assert is_valid_cell_count("tic-tac-toe", 27)  # 3x3x3
    assert not is_valid_cell_count("tic-tac-toe", 36)  # not a cube
    # A game with no registered judge keeps any size the wire schema allowed.
    assert is_valid_cell_count("mystery-game", 63)
