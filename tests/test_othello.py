"""Othello legality and outcome, and the registry wiring that puts them in front of a room."""

import pytest

from app.modules.rooms import othello
from app.modules.rooms.validators import InvalidMove, judge_outcome, validate_move

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
        finished, _winner = othello.outcome(moves, first_seat, CELLS)  # type: ignore[misc]
        if finished:
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
    _finished, seat_when_zero_opens = othello.outcome(moves, 0, CELLS)  # type: ignore[misc]
    _finished, seat_when_one_opens = othello.outcome(moves, 1, CELLS)  # type: ignore[misc]
    if seat_when_zero_opens is None:
        assert seat_when_one_opens is None  # a draw is a draw whoever opened
    else:
        assert seat_when_one_opens == 1 - seat_when_zero_opens


def test_a_game_in_progress_is_not_over():
    assert othello.outcome([], 0, CELLS) == (False, None)


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
