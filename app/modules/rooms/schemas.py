from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, Field

from app.camel import CamelModel
from app.config import settings
from app.modules.rooms.constants import RoomOutcome, RoomStatus

_MAX_GAME_ID = 40
_MAX_COLOUR = 32
_MAX_NAME = 24
MAX_TOKEN = 128
# Generous cell bound so future games with bigger boards fit; the room's own cell_count is the real
# limit the default legality check applies. -1 is the reserved pass sentinel (games that allow it
# validate it themselves).
_MIN_MOVE = -1
_MAX_MOVE = 4095
_MAX_CELLS = 4096
# A clock has to leave time to look at the board.
_MIN_MOVE_LIMIT = 5


def _within_room_lifetime(value: int | None) -> int | None:
    """Keeps a turn deadline inside the room's own lifetime.

    A clock longer than `ROOM_TTL_SECONDS` gives the player on turn a deadline the room does not
    live to see: every read 410s and the game disappears mid-turn. Read from config on each
    validation rather than baked in, so lowering the TTL tightens this with it.
    """
    ttl = settings.room_ttl_seconds
    if value is not None and value > ttl:
        raise ValueError(f"cannot exceed the room lifetime of {ttl} seconds")
    return value


MoveLimitSeconds = Annotated[
    int | None, Field(ge=_MIN_MOVE_LIMIT), AfterValidator(_within_room_lifetime)
]


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
    move_limit_seconds: MoveLimitSeconds = None


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
    move_limit_seconds: MoveLimitSeconds = None


class JoinRoomRequest(CamelModel):
    name: str = Field(default="", max_length=_MAX_NAME)
    colour: str = Field(min_length=1, max_length=_MAX_COLOUR)


class ProfileRequest(CamelModel):
    token: str = Field(min_length=1, max_length=MAX_TOKEN)
    name: str = Field(default="", max_length=_MAX_NAME)
    colour: str = Field(min_length=1, max_length=_MAX_COLOUR)


class SettingsRequest(RoomOptions):
    """A full replacement of the room's settings, not a patch.

    Every option is required, so a body that omits one is a 422 rather than a silent reset of the
    field it left out — inherited defaults would quietly hand back seat 0, a closed room and no
    clock to a client that only meant to change the clock.
    """

    token: str = Field(min_length=1, max_length=MAX_TOKEN)
    first_seat: int = Field(ge=0, le=1)
    is_open: bool
    move_limit_seconds: MoveLimitSeconds
    # Only a game whose board size can change sends this; left off, the room keeps its current size.
    # Changing it resets the board, so the service allows it only before a game has started.
    cell_count: int | None = Field(default=None, ge=1, le=_MAX_CELLS)


class TokenRequest(CamelModel):
    """Just proof of a seat, for the actions that need nothing else."""

    token: str = Field(min_length=1, max_length=MAX_TOKEN)


class MoveRequest(CamelModel):
    token: str = Field(min_length=1, max_length=MAX_TOKEN)
    move: int = Field(ge=_MIN_MOVE, le=_MAX_MOVE)
    expected_version: int = Field(ge=0)
    # The client runs the engine and knows when the game is over; the server just records it.
    finished: bool = False
    # Whether that move won it, as opposed to filling the last square for a draw.
    won: bool = False


class AimRequest(CamelModel):
    """A move aimed but not committed, kept so the clock plays it rather than forfeiting the game.

    A real cell only — the pass sentinel is not something a player aims, so ``move`` starts at 0.
    """

    token: str = Field(min_length=1, max_length=MAX_TOKEN)
    move: int = Field(ge=0, le=_MAX_MOVE)
    expected_version: int = Field(ge=0)


# Returned only to the seat that owns it, on create/join — carries the secret token.
class RoomCredentials(CamelModel):
    code: str
    game_id: str
    cell_count: int
    seat: int
    token: str
    status: RoomStatus


# The public view, safe to poll — seat tokens are never included.
class RoomState(CamelModel):
    code: str
    game_id: str
    cell_count: int
    moves: list[int]
    seats: list[SeatOut]
    status: RoomStatus
    version: int
    expires_at: datetime
    # Which seat opened this game, so a client can work out whose turn it is.
    first_seat: int
    # Whose room it is: the seat that may change the settings and start a game.
    owner_seat: int
    is_open: bool
    move_limit_seconds: int | None
    # When the player on turn runs out of time. Null when the room has no clock running.
    turn_ends_at: datetime | None
    outcome: RoomOutcome | None
    winner_seat: int | None


class MoveResult(CamelModel):
    version: int
    moves: list[int]
    status: RoomStatus
    # The accepted move restarts the clock, so the mover's own reply carries the new deadline;
    # without it their client keeps counting the turn it just ended. Null when there is no clock.
    turn_ends_at: datetime | None
    outcome: RoomOutcome | None
    winner_seat: int | None
