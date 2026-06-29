import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.rate_limit import limiter
from app.subscriptions import service
from app.subscriptions.schemas import SubscriptionCreate, SubscriptionPatch

router = APIRouter()
# uvicorn's configured logger so these land alongside the access logs.
logger = logging.getLogger("uvicorn.error")

_RATE_LIMITED: dict[int | str, dict[str, Any]] = {429: {"description": "Rate limit exceeded."}}

# The service layer returns (status_code, body) so it stays framework-agnostic and unit-testable
# without a request; the router just forwards that to the client.


@router.post(
    "",
    summary="Subscribe to recall alerts",
    description=(
        "Public, rate-limited to 5/min per IP. Creates a pending subscription and sends a "
        "double opt-in email. Always returns a uniform 200 regardless of the address's prior "
        "state, so the response never reveals whether an email is already registered "
        "(422 on validation error)."
    ),
    responses=_RATE_LIMITED,
)
@limiter.limit("5/minute")
def create_subscription(
    request: Request,
    payload: SubscriptionCreate,
    session: Session = Depends(get_session),
) -> JSONResponse:
    status_code, body = service.create(payload, session)
    return JSONResponse(status_code=status_code, content=body)


@router.get(
    "/confirm",
    summary="Confirm a subscription",
    description="Activates a pending subscription via the raw token from the opt-in email link.",
)
def confirm_subscription(
    token: str = Query(..., description="Raw confirmation token from the opt-in email."),
    session: Session = Depends(get_session),
) -> JSONResponse:
    status_code, body = service.confirm(token, session)
    return JSONResponse(status_code=status_code, content=body)


@router.get(
    "/manage",
    summary="Read subscription preferences",
    description="Returns the filter criteria and a masked email for a management token.",
)
def get_subscription(
    token: str = Query(..., description="Management token from an email link."),
    session: Session = Depends(get_session),
) -> JSONResponse:
    status_code, body = service.get_manage(token, session)
    return JSONResponse(status_code=status_code, content=body)


@router.patch(
    "/manage",
    summary="Update subscription preferences",
    description="Partial update of the filter criteria; omitted fields are unchanged.",
)
def patch_subscription(
    payload: SubscriptionPatch,
    token: str = Query(..., description="Management token from an email link."),
    session: Session = Depends(get_session),
) -> JSONResponse:
    status_code, body = service.patch_manage(token, payload, session)
    return JSONResponse(status_code=status_code, content=body)


@router.post(
    "/unsubscribe",
    summary="Unsubscribe",
    description="Transitions a subscription to unsubscribed via its management token.",
)
def unsubscribe_subscription(
    token: str = Query(..., description="Management token from an email link."),
    session: Session = Depends(get_session),
) -> JSONResponse:
    status_code, body = service.unsubscribe(token, session)
    return JSONResponse(status_code=status_code, content=body)
