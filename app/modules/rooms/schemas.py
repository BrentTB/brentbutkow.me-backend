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


class SeatOut(CamelModel):
    seat: int
    name: str
    colour: str
    joined: bool


class CreateRoomRequest(CamelModel):
    game_id: str = Field(min_length=1, max_length=_MAX_GAME_ID)
    name: str = Field(default="", max_length=_MAX_NAME)
    colour: str = Field(min_length=1, max_length=_MAX_COLOUR)
    cell_count: int = Field(ge=1, le=_MAX_CELLS)


class JoinRoomRequest(CamelModel):
    name: str = Field(default="", max_length=_MAX_NAME)
    colour: str = Field(min_length=1, max_length=_MAX_COLOUR)


class ProfileRequest(CamelModel):
    token: str = Field(min_length=1, max_length=_MAX_TOKEN)
    name: str = Field(default="", max_length=_MAX_NAME)
    colour: str = Field(min_length=1, max_length=_MAX_COLOUR)


class MoveRequest(CamelModel):
    token: str = Field(min_length=1, max_length=_MAX_TOKEN)
    move: int = Field(ge=_MIN_MOVE, le=_MAX_MOVE)
    expected_version: int = Field(ge=0)
    # The client runs the engine and knows when the game is over; the server just records it.
    finished: bool = False


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


class MoveResult(CamelModel):
    version: int
    moves: list[int]
    status: str
