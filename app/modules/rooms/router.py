from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.modules.rooms import service
from app.modules.rooms.models import Room
from app.modules.rooms.schemas import (
    CreateRoomRequest,
    JoinRoomRequest,
    MoveRequest,
    MoveResult,
    ProfileRequest,
    RoomCredentials,
    RoomState,
    SeatOut,
)
from app.rate_limit import limiter

router = APIRouter()

_RATE_LIMITED: dict[int | str, dict[str, Any]] = {429: {"description": "Rate limit exceeded."}}


def _state(room: Room) -> RoomState:
    # Seat tokens never leave the server — only seat number, colour, and join status.
    seats = [
        SeatOut(
            seat=int(s["seat"]),
            name=s.get("name", ""),
            colour=s["colour"],
            joined=bool(s["joined"]),
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
    )
    return RoomCredentials(
        code=room.code,
        game_id=room.game_id,
        cell_count=room.cell_count,
        seat=0,
        token=token,
        status=room.status,
    )


@router.post(
    "/{code}/join",
    response_model=RoomCredentials,
    summary="Join a room by code",
    description="Claims the open second seat. Returns your seat token.",
    responses=_RATE_LIMITED,
)
@limiter.limit("20/minute")
def join_room(
    request: Request,
    code: str,
    body: JoinRoomRequest,
    session: Session = Depends(get_session),
) -> RoomCredentials:
    room, token = service.join_room(session, code=code, name=body.name, colour=body.colour)
    return RoomCredentials(
        code=room.code,
        game_id=room.game_id,
        cell_count=room.cell_count,
        seat=1,
        token=token,
        status=room.status,
    )


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
    code: str,
    body: ProfileRequest,
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
    description="The current move list and seats, polled by both clients. Higher limit for polls.",
    responses=_RATE_LIMITED,
)
@limiter.limit("240/minute")
def get_room(
    request: Request,
    code: str,
    session: Session = Depends(get_session),
) -> RoomState:
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
    code: str,
    body: MoveRequest,
    session: Session = Depends(get_session),
) -> MoveResult:
    room = service.append_move(
        session,
        code=code,
        token=body.token,
        move=body.move,
        expected_version=body.expected_version,
        finished=body.finished,
    )
    return MoveResult(version=len(room.moves), moves=list(room.moves), status=room.status)
