from typing import Any

from fastapi import APIRouter, Depends, Header, Path, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.modules.rooms import service
from app.modules.rooms.models import Room
from app.modules.rooms.schemas import (
    CreateRoomRequest,
    JoinRoomRequest,
    MatchmakeRequest,
    MoveRequest,
    MoveResult,
    ProfileRequest,
    RoomCredentials,
    RoomState,
    SeatOut,
    TokenRequest,
)
from app.rate_limit import limiter

router = APIRouter()

_RATE_LIMITED: dict[int | str, dict[str, Any]] = {429: {"description": "Rate limit exceeded."}}

# Every code the server mints is this long, so anything else cannot name a room.
RoomCode = Path(min_length=service.CODE_LENGTH, max_length=service.CODE_LENGTH)


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
        status=room.status,
        version=len(room.moves),
        expires_at=room.expires_at,
        first_seat=room.first_seat,
        is_open=room.is_open,
        move_limit_seconds=room.move_limit_seconds,
        turn_ends_at=service.turn_deadline(room),
        outcome=room.outcome,
        winner_seat=room.winner_seat,
    )


def _credentials(room: Room, seat: int, token: str) -> RoomCredentials:
    return RoomCredentials(
        code=room.code,
        game_id=room.game_id,
        cell_count=room.cell_count,
        seat=seat,
        token=token,
        status=room.status,
    )


@router.post(
    "",
    response_model=RoomCredentials,
    summary="Open a multiplayer room",
    description="Creates a room for the given game and claims seat 0. Returns your seat token.",
    responses=_RATE_LIMITED,
)
@limiter.limit("20/minute")
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
        "nobody to match with. Returns your seat token either way."
    ),
    responses=_RATE_LIMITED,
)
@limiter.limit("20/minute")
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
        move_limit_seconds=body.move_limit_seconds,
    )
    seat = service.seat_for_token(room.seats, token)
    return _credentials(room, seat, token)


@router.post(
    "/{code}/join",
    response_model=RoomCredentials,
    summary="Join a room by code",
    description="Claims the room's open seat. Returns your seat token.",
    responses=_RATE_LIMITED,
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
        "leaving before it starts just reopens the seat."
    ),
    responses=_RATE_LIMITED,
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
    "/{code}/rematch",
    response_model=RoomState,
    summary="Play again with the same opponent",
    description="Clears the board in this room and hands the opening move to the other seat.",
    responses=_RATE_LIMITED,
)
@limiter.limit("30/minute")
def rematch(
    request: Request,
    body: TokenRequest,
    code: str = RoomCode,
    session: Session = Depends(get_session),
) -> RoomState:
    return _state(service.rematch(session, code=code, token=body.token))


@router.post(
    "/{code}/profile",
    response_model=RoomState,
    summary="Update your seat's name and colour",
    description="Changes the caller's own name and colour. The opponent sees it on their next poll",
    responses=_RATE_LIMITED,
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
        "`X-Seat-Token` so the room knows you are still here; without it the read is anonymous."
    ),
    responses=_RATE_LIMITED,
)
@limiter.limit("240/minute")
def get_room(
    request: Request,
    code: str = RoomCode,
    x_seat_token: str = Header(default=""),
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
        "Appends a move after checking the seat token, that it is your turn, that your version is "
        "current, and that the move is legal for the game. Rejects otherwise (403/409/422)."
    ),
    responses=_RATE_LIMITED,
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
        status=room.status,
        outcome=room.outcome,
        winner_seat=room.winner_seat,
    )
