import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import require_bearer
from app.db import get_session
from app.modules.contact.models import Message
from app.modules.contact.schemas import ContactResult, ContactSubmission, MessageOut
from app.modules.contact.service import create_message, list_messages
from app.rate_limit import client_ip, limiter

router = APIRouter()
# uvicorn's configured logger so these land alongside the access logs.
logger = logging.getLogger("uvicorn.error")

_RATE_LIMITED: dict[int | str, dict[str, Any]] = {429: {"description": "Rate limit exceeded."}}

# Reject submissions faster than a human could plausibly fill the form.
_MIN_ELAPSED_MS = 2000


@router.post(
    "",
    response_model=ContactResult,
    summary="Send a contact message",
    description="Public, rate-limited to 5/min per IP. Stores the message plus coarse context.",
    responses=_RATE_LIMITED,
)
@limiter.limit("5/minute")
def submit_contact(
    request: Request,
    submission: ContactSubmission,
    session: Session = Depends(get_session),
) -> ContactResult:
    ip = client_ip(request)
    ua = request.headers.get("user-agent")
    accept_language = request.headers.get("accept-language")

    def store(*, is_bot: bool = False, bot_reason: str | None = None) -> None:
        create_message(
            session,
            submission,
            user_agent=ua,
            accept_language=accept_language,
            ip_address=ip,
            is_bot=is_bot,
            bot_reason=bot_reason,
        )

    # Spam traps return ok so the bot gets no signal, but the submission is kept (capped) under
    # is_bot so it can be inspected later.
    if submission.website:
        store(is_bot=True, bot_reason="honeypot")
        logger.info("contact honeypot tripped from ip=%s — stored as bot", ip)
        return ContactResult(status="ok")
    if submission.elapsed_ms is not None and submission.elapsed_ms < _MIN_ELAPSED_MS:
        store(is_bot=True, bot_reason="timetrap")
        logger.info(
            "contact time-trap tripped (%dms) from ip=%s — stored as bot", submission.elapsed_ms, ip
        )
        return ContactResult(status="ok")

    store()
    logger.info(
        "contact message stored from ip=%s named=%s with_email=%s",
        ip,
        bool(submission.name),
        bool(submission.email),
    )
    return ContactResult(status="ok")


@router.get(
    "",
    response_model=list[MessageOut],
    summary="List contact messages",
    description="Bearer-protected — stored messages, newest first.",
    dependencies=[Depends(require_bearer)],
    responses={401: {"description": "Missing or invalid bearer token."}},
)
def get_messages(session: Session = Depends(get_session)) -> list[Message]:
    return list_messages(session)
