from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.modules.contact.models import Message
from app.modules.contact.schemas import ContactSubmission

_LIST_LIMIT = 100
# Cap stored spam so the trap can't grow the table without bound.
_MAX_BOT_ROWS = 200
# Cap the whole table too: submissions are unauthenticated, so a sender who stays under the bot
# traps (slow + blank honeypot) could otherwise grow it without bound, throttled only per-IP.
_MAX_ROWS = 5000


def create_message(
    session: Session,
    submission: ContactSubmission,
    *,
    user_agent: str | None,
    accept_language: str | None,
    ip_address: str | None,
    is_bot: bool = False,
    bot_reason: str | None = None,
) -> Message:
    message = Message(
        message=submission.message,
        name=submission.name,
        email=submission.email,
        timezone=submission.timezone,
        locale=submission.locale,
        referrer=submission.referrer,
        user_agent=user_agent,
        accept_language=accept_language,
        ip_address=ip_address,
        is_bot=is_bot,
        bot_reason=bot_reason,
    )
    session.add(message)
    session.commit()
    session.refresh(message)
    _prune_messages(session)
    if is_bot:
        _prune_bot_messages(session)
    return message


def _prune_messages(session: Session) -> None:
    # Enforce the overall row cap. Bots are low-value, so make room by evicting the oldest of them
    # first; only delete real messages as a last resort, once no bots are left to remove.
    over = (session.scalar(select(func.count()).select_from(Message)) or 0) - _MAX_ROWS
    if over <= 0:
        return
    removed = _delete_oldest(session, over, is_bot=True)
    if removed < over:
        _delete_oldest(session, over - removed, is_bot=False)
    session.commit()


def _delete_oldest(session: Session, count: int, *, is_bot: bool) -> int:
    # Delete up to `count` oldest rows of the given kind; return how many were removed.
    ids = list(
        session.scalars(
            select(Message.id)
            .where(Message.is_bot.is_(is_bot))
            .order_by(Message.created_at.asc(), Message.id.asc())
            .limit(count)
        ).all()
    )
    if ids:
        session.execute(delete(Message).where(Message.id.in_(ids)))
    return len(ids)


def _prune_bot_messages(session: Session) -> None:
    # Keep only the most recent _MAX_BOT_ROWS spam rows; delete the rest.
    keep = (
        select(Message.id)
        .where(Message.is_bot.is_(True))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(_MAX_BOT_ROWS)
    )
    session.execute(delete(Message).where(Message.is_bot.is_(True), Message.id.not_in(keep)))
    session.commit()


def list_messages(session: Session) -> list[Message]:
    return list(
        session.scalars(
            select(Message)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(_LIST_LIMIT)
        ).all()
    )
