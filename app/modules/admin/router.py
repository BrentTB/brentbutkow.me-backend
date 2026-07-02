import hmac
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.auth import issue_admin_token, require_admin
from app.config import settings
from app.db import get_session
from app.modules.admin import service
from app.modules.admin.schemas import (
    AdminLoginRequest,
    AdminLoginResult,
    AdminMessageUpdate,
    AdminOverview,
    AdminSubscriptionUpdate,
    MessageListResult,
    MessageOut,
    ScoreAdminOut,
    ScoreListResult,
    SubscriptionAdminOut,
    SubscriptionListResult,
)
from app.rate_limit import limiter

router = APIRouter()

_RATE_LIMITED: dict[int | str, dict[str, Any]] = {429: {"description": "Rate limit exceeded."}}
_UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid admin session token."}
}

SubscriptionStatus = Literal["pending_confirmation", "active", "paused", "unsubscribed"]


@router.post(
    "/login",
    response_model=AdminLoginResult,
    summary="Admin login",
    description="Exchange ADMIN_PASSWORD for a short-lived session token. Rate-limited.",
    responses={401: {"description": "Invalid password."}, **_RATE_LIMITED},
)
@limiter.limit("5/minute")
def login(request: Request, body: AdminLoginRequest) -> AdminLoginResult:
    expected = settings.admin_password
    # Fail closed when no password is configured; otherwise constant-time compare. A wrong password
    # and an unconfigured server are intentionally indistinguishable to the caller.
    if not expected or not hmac.compare_digest(
        body.password.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    token, expires_at = issue_admin_token()
    return AdminLoginResult(token=token, expires_at=expires_at)


@router.get(
    "/overview",
    response_model=AdminOverview,
    summary="Admin dashboard overview",
    description="One-call summary: message, subscription, recall, and ingest counts.",
    dependencies=[Depends(require_admin)],
    responses=_UNAUTHORIZED,
)
def overview(session: Session = Depends(get_session)) -> AdminOverview:
    return service.build_overview(session)


@router.get(
    "/messages",
    response_model=MessageListResult,
    summary="List contact messages",
    description="Paginated, newest first. Spam/bot rows are excluded unless includeBots=true. "
    "Filter by read state with seen=true (read) or seen=false (unread); omit for all.",
    dependencies=[Depends(require_admin)],
    responses=_UNAUTHORIZED,
)
def messages(
    session: Session = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_bots: bool = Query(default=False, alias="includeBots"),
    seen: bool | None = Query(default=None),
) -> MessageListResult:
    items, total = service.list_messages(
        session, limit=limit, offset=offset, include_bots=include_bots, seen=seen
    )
    return MessageListResult(items=[MessageOut.model_validate(m) for m in items], total=total)


@router.patch(
    "/messages/{message_id}",
    response_model=MessageOut,
    summary="Edit a contact message",
    description="Toggle the seen (read) flag on a message. Applied directly.",
    dependencies=[Depends(require_admin)],
    responses={**_UNAUTHORIZED, 404: {"description": "Message not found."}},
)
def edit_message(
    message_id: int,
    body: AdminMessageUpdate,
    session: Session = Depends(get_session),
) -> MessageOut:
    row = service.update_message(session, message_id, body)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return MessageOut.model_validate(row)


@router.get(
    "/subscriptions",
    response_model=SubscriptionListResult,
    summary="List subscriptions",
    description="Paginated, newest first. Optionally filter by status.",
    dependencies=[Depends(require_admin)],
    responses=_UNAUTHORIZED,
)
def subscriptions(
    session: Session = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    # Aliased to `status` on the wire; the local name avoids shadowing fastapi's `status` module.
    status_filter: SubscriptionStatus | None = Query(default=None, alias="status"),
) -> SubscriptionListResult:
    items, total = service.list_subscriptions(
        session, limit=limit, offset=offset, status=status_filter
    )
    return SubscriptionListResult(
        items=[SubscriptionAdminOut.model_validate(s) for s in items], total=total
    )


@router.patch(
    "/subscriptions/{subscription_id}",
    response_model=SubscriptionAdminOut,
    summary="Edit a subscription",
    description="Revoke (unsubscribed), suspend (paused), reactivate (active), and/or edit filter "
    "criteria. Applied directly — no subscriber confirmation.",
    dependencies=[Depends(require_admin)],
    responses={**_UNAUTHORIZED, 404: {"description": "Subscription not found."}},
)
def edit_subscription(
    subscription_id: uuid.UUID,
    body: AdminSubscriptionUpdate,
    session: Session = Depends(get_session),
) -> SubscriptionAdminOut:
    row = service.update_subscription(session, subscription_id, body)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    return SubscriptionAdminOut.model_validate(row)


@router.get(
    "/nullspace",
    response_model=ScoreListResult,
    summary="List Null Space scores",
    description="Paginated, newest first. Filter by flagged status (omit for all runs).",
    dependencies=[Depends(require_admin)],
    responses=_UNAUTHORIZED,
)
def nullspace(
    session: Session = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    flagged: bool | None = Query(default=None),
) -> ScoreListResult:
    items, total = service.list_scores(session, limit=limit, offset=offset, flagged=flagged)
    return ScoreListResult(items=[ScoreAdminOut.model_validate(s) for s in items], total=total)
