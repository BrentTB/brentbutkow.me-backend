from fastapi import APIRouter, Depends, Header, Path, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.modules.rooms import service
from app.modules.rooms.constants import RoomOutcome, RoomStatus
from app.modules.rooms.models import Room
from app.modules.rooms.schemas import (
    MAX_TOKEN,
    CreateRoomRequest,
    JoinRoomRequest,
    MatchmakeRequest,
    MoveRequest,
    MoveResult,
    ProfileRequest,
    RoomCredentials,
    RoomState,
    SeatOut,
    SettingsRequest,
    TokenRequest,
)
from app.openapi import RATE_LIMITED
from app.rate_limit import limiter

router = APIRouter()

# Length and charset both, from the alphabet codes are drawn out of: a code no server could have
# minted is a 422 before it costs a query, and third parties read the alphabet off the schema.
RoomCode = Path(
    min_length=service.CODE_LENGTH,
    max_length=service.CODE_LENGTH,
    pattern=f"^[{service.CODE_ALPHABET}]{{{service.CODE_LENGTH}}}$",
)

# A stricter, wider window alongside the per-minute limit: unauthenticated room creation with a
# day-long TTL is otherwise an open invitation to leave a day's worth of rows for matchmaking to
# walk through.
_CREATE_LIMITS = "20/minute;100/hour"


def _status(room: Room) -> RoomStatus:
    """The room's status as the enum the responses declare.

    The column is a plain string with a CHECK behind it, so this is where a value that somehow got
    past the constraint becomes a loud 500 rather than a status no client knows how to read.
    """
    return RoomStatus(room.status)


def _outcome(room: Room) -> RoomOutcome | None:
    return RoomOutcome(room.outcome) if room.outcome is not None else None


def _state(room: Room) -> RoomState:
    # Seat tokens never leave the server: only seat number, name, colour, and who is here.
    now = service.now_utc()
    seats = [
        SeatOut(
            seat=int(s["seat"]),
            name=s.get("name", ""),
            colour=s["colour"],
            joined=service.seat_present(s, now),
        )
        for s in room.seats
    ]
    return RoomState(
        code=room.code,
        game_id=room.game_id,
        cell_count=room.cell_count,
        moves=list(room.moves),
        seats=seats,
        status=_status(room),
        version=len(room.moves),
        expires_at=room.expires_at,
        first_seat=room.first_seat,
        owner_seat=room.owner_seat,
        is_open=room.is_open,
        move_limit_seconds=room.move_limit_seconds,
        turn_ends_at=service.turn_deadline(room),
        outcome=_outcome(room),
        winner_seat=room.winner_seat,
    )


def _credentials(room: Room, seat: int, token: str) -> RoomCredentials:
    return RoomCredentials(
        code=room.code,
        game_id=room.game_id,
        cell_count=room.cell_count,
        seat=seat,
        token=token,
        status=_status(room),
    )


@router.post(
    "",
    response_model=RoomCredentials,
    summary="Open a multiplayer room",
    description=(
        "Creates a room for the given game and claims seat 0, which owns the room. Returns your "
        "seat token. Public, rate-limited to 20/min and 100/hour per IP."
    ),
    responses=RATE_LIMITED,
)
@limiter.limit(_CREATE_LIMITS)
def create_room(
    request: Request,
    body: CreateRoomRequest,
    session: Session = Depends(get_session),
) -> RoomCredentials:
    room, token = service.create_room(
        session,
        game_id=body.game_id,
        name=body.name,
        colour=body.colour,
        cell_count=body.cell_count,
        first_seat=body.first_seat,
        is_open=body.is_open,
        move_limit_seconds=body.move_limit_seconds,
    )
    return _credentials(room, 0, token)


@router.post(
    "/matchmake",
    response_model=RoomCredentials,
    summary="Find a game against anyone",
    description=(
        "Joins the longest-waiting open room for this game, or opens one and waits when there is "
        "nobody to match with. Returns your seat token either way. `firstSeat` and "
        "`moveLimitSeconds` apply only to a room this opens — joining somebody means playing by "
        "the settings they chose. Public, rate-limited to 20/min and 100/hour per IP."
    ),
    responses=RATE_LIMITED,
)
@limiter.limit(_CREATE_LIMITS)
def matchmake(
    request: Request,
    body: MatchmakeRequest,
    session: Session = Depends(get_session),
) -> RoomCredentials:
    room, token = service.matchmake(
        session,
        game_id=body.game_id,
        cell_count=body.cell_count,
        name=body.name,
        colour=body.colour,
        first_seat=body.first_seat,
        move_limit_seconds=body.move_limit_seconds,
    )
    seat = service.seat_for_token(room.seats, token)
    return _credentials(room, seat, token)


@router.post(
    "/{code}/join",
    response_model=RoomCredentials,
    summary="Join a room by code",
    description=(
        "Claims the room's open seat. Returns your seat token. 409s while a game is running, and "
        "when both seats are taken; a room whose last game ended clears as you sit down."
    ),
    responses=RATE_LIMITED,
)
@limiter.limit("20/minute")
def join_room(
    request: Request,
    body: JoinRoomRequest,
    code: str = RoomCode,
    session: Session = Depends(get_session),
) -> RoomCredentials:
    room, token = service.join_room(session, code=code, name=body.name, colour=body.colour)
    seat = service.seat_for_token(room.seats, token)
    return _credentials(room, seat, token)


@router.post(
    "/{code}/leave",
    response_model=RoomState,
    summary="Give up your seat",
    description=(
        "Frees the caller's seat. Walking out of a game in progress hands it to the other player; "
        "leaving before it starts, or after it has ended, just reopens the seat."
    ),
    responses=RATE_LIMITED,
)
@limiter.limit("30/minute")
def leave_room(
    request: Request,
    body: TokenRequest,
    code: str = RoomCode,
    session: Session = Depends(get_session),
) -> RoomState:
    return _state(service.leave_room(session, code=code, token=body.token))


@router.post(
    "/{code}/start",
    response_model=RoomState,
    summary="Start a game in this room",
    description=(
        "Clears the board and begins play. Only the room's owner may start it, and only with both "
        "players present; refuses while a game is already running."
    ),
    responses=RATE_LIMITED,
)
@limiter.limit("30/minute")
def start_game(
    request: Request,
    body: TokenRequest,
    code: str = RoomCode,
    session: Session = Depends(get_session),
) -> RoomState:
    return _state(service.start_game(session, code=code, token=body.token))


@router.post(
    "/{code}/settings",
    response_model=RoomState,
    summary="Change the room's settings between games",
    description=(
        "Replaces the opening seat, clock and open flag — all three are required, so a partial "
        "body is a 422 rather than a reset of what it left out. Only the room's owner may call it, "
        "and only between games: before the first one, and after any game has ended."
    ),
    responses=RATE_LIMITED,
)
@limiter.limit("30/minute")
def update_settings(
    request: Request,
    body: SettingsRequest,
    code: str = RoomCode,
    session: Session = Depends(get_session),
) -> RoomState:
    room = service.update_settings(
        session,
        code=code,
        token=body.token,
        first_seat=body.first_seat,
        is_open=body.is_open,
        move_limit_seconds=body.move_limit_seconds,
    )
    return _state(room)


@router.post(
    "/{code}/profile",
    response_model=RoomState,
    summary="Update your seat's name and colour",
    description="Changes the caller's own name and colour. The opponent sees it on their next poll",
    responses=RATE_LIMITED,
)
@limiter.limit("60/minute")
def update_profile(
    request: Request,
    body: ProfileRequest,
    code: str = RoomCode,
    session: Session = Depends(get_session),
) -> RoomState:
    room = service.update_profile(
        session, code=code, token=body.token, name=body.name, colour=body.colour
    )
    return _state(room)


@router.get(
    "/{code}",
    response_model=RoomState,
    summary="Read room state",
    description=(
        "The current move list and seats, polled by both clients. Send your seat token as "
        "`X-Seat-Token` so the room knows you are still here; without it the read is anonymous. "
        "Reading also settles the game: a turn whose clock ran out, or an opponent gone long "
        "enough to forfeit, is decided here."
    ),
    responses=RATE_LIMITED,
)
@limiter.limit("240/minute")
def get_room(
    request: Request,
    code: str = RoomCode,
    x_seat_token: str = Header(default="", max_length=MAX_TOKEN),
    session: Session = Depends(get_session),
) -> RoomState:
    # A player's own poll doubles as their heartbeat, so presence costs no extra request.
    if x_seat_token:
        return _state(service.touch_seat(session, code=code, token=x_seat_token))
    return _state(service.get_room(session, code))


@router.post(
    "/{code}/move",
    response_model=MoveResult,
    summary="Submit a move",
    description=(
        "Appends a move after checking the seat token (403), that your version is current (409), "
        "that it is your turn (403), and that the move is legal for the game (422). Whether the "
        "move ended the game is the server's own reading wherever that game has a judge — for "
        "`tic-tac-toe` the `finished` and `won` fields are ignored. Games with no judge keep the "
        "client's verdict. Resubmitting the move the server already applied returns that state "
        "rather than an error, so a lost reply is safe to retry."
    ),
    responses=RATE_LIMITED,
)
@limiter.limit("120/minute")
def submit_move(
    request: Request,
    body: MoveRequest,
    code: str = RoomCode,
    session: Session = Depends(get_session),
) -> MoveResult:
    room = service.append_move(
        session,
        code=code,
        token=body.token,
        move=body.move,
        expected_version=body.expected_version,
        finished=body.finished,
        won=body.won,
    )
    return MoveResult(
        version=len(room.moves),
        moves=list(room.moves),
        status=_status(room),
        # The move restarted the clock, so the mover gets the new deadline in the same reply.
        turn_ends_at=service.turn_deadline(room),
        outcome=_outcome(room),
        winner_seat=room.winner_seat,
    )
