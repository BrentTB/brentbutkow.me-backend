from datetime import datetime

from pydantic import Field

from app.camel import CamelModel

_MAX_GAME_ID = 40
_MAX_COLOUR = 32
_MAX_NAME = 24
_MAX_TOKEN = 128
# Generous cell bound so future games with bigger boards fit; the room's own cell_count is the real
# limit the default legality check applies. -1 is the reserved pass sentinel (games that allow it
# validate it themselves).
_MIN_MOVE = -1
_MAX_MOVE = 4095
_MAX_CELLS = 4096
# A clock has to leave time to look at the board, and a day is already the room's whole TTL.
_MIN_MOVE_LIMIT = 5
_MAX_MOVE_LIMIT = 86400


class SeatOut(CamelModel):
    seat: int
    name: str
    colour: str
    # Whether that player is here: false once they leave or stop reading the room.
    joined: bool


# Settings a room keeps for its whole life, chosen by whoever opens it.
class RoomOptions(CamelModel):
    first_seat: int = Field(default=0, ge=0, le=1)
    is_open: bool = False
    move_limit_seconds: int | None = Field(default=None, ge=_MIN_MOVE_LIMIT, le=_MAX_MOVE_LIMIT)


class CreateRoomRequest(RoomOptions):
    game_id: str = Field(min_length=1, max_length=_MAX_GAME_ID)
    name: str = Field(default="", max_length=_MAX_NAME)
    colour: str = Field(min_length=1, max_length=_MAX_COLOUR)
    cell_count: int = Field(ge=1, le=_MAX_CELLS)


class MatchmakeRequest(CamelModel):
    game_id: str = Field(min_length=1, max_length=_MAX_GAME_ID)
    name: str = Field(default="", max_length=_MAX_NAME)
    colour: str = Field(min_length=1, max_length=_MAX_COLOUR)
    cell_count: int = Field(ge=1, le=_MAX_CELLS)
    # These apply only when nobody is waiting and this player opens the room instead. Joining
    # somebody means playing by the settings they already chose.
    first_seat: int = Field(default=0, ge=0, le=1)
    move_limit_seconds: int | None = Field(default=None, ge=_MIN_MOVE_LIMIT, le=_MAX_MOVE_LIMIT)


class JoinRoomRequest(CamelModel):
    name: str = Field(default="", max_length=_MAX_NAME)
    colour: str = Field(min_length=1, max_length=_MAX_COLOUR)


class ProfileRequest(CamelModel):
    token: str = Field(min_length=1, max_length=_MAX_TOKEN)
    name: str = Field(default="", max_length=_MAX_NAME)
    colour: str = Field(min_length=1, max_length=_MAX_COLOUR)


class SettingsRequest(RoomOptions):
    token: str = Field(min_length=1, max_length=_MAX_TOKEN)


class TokenRequest(CamelModel):
    """Just proof of a seat, for the actions that need nothing else."""

    token: str = Field(min_length=1, max_length=_MAX_TOKEN)


class MoveRequest(CamelModel):
    token: str = Field(min_length=1, max_length=_MAX_TOKEN)
    move: int = Field(ge=_MIN_MOVE, le=_MAX_MOVE)
    expected_version: int = Field(ge=0)
    # The client runs the engine and knows when the game is over; the server just records it.
    finished: bool = False
    # Whether that move won it, as opposed to filling the last square for a draw.
    won: bool = False


# Returned only to the seat that owns it, on create/join — carries the secret token.
class RoomCredentials(CamelModel):
    code: str
    game_id: str
    cell_count: int
    seat: int
    token: str
    status: str


# The public view, safe to poll — seat tokens are never included.
class RoomState(CamelModel):
    code: str
    game_id: str
    cell_count: int
    moves: list[int]
    seats: list[SeatOut]
    status: str
    version: int
    expires_at: datetime
    # Which seat opened this game, so a client can work out whose turn it is.
    first_seat: int
    is_open: bool
    move_limit_seconds: int | None
    # When the player on turn runs out of time. Null when the room has no clock running.
    turn_ends_at: datetime | None
    outcome: str | None
    winner_seat: int | None


class MoveResult(CamelModel):
    version: int
    moves: list[int]
    status: str
    outcome: str | None
    winner_seat: int | None
