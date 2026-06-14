from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.modules.contact.models import Message
from app.modules.contact.schemas import ContactSubmission

_LIST_LIMIT = 100
# Cap stored spam so the trap can't grow the table without bound.
_MAX_BOT_ROWS = 200


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
    if is_bot:
        _prune_bot_messages(session)
    return message


def _prune_bot_messages(session: Session) -> None:
    # Keep only the most recent _MAX_BOT_ROWS spam rows; delete the rest.
    keep = (
        select(Message.id)
        .where(Message.is_bot.is_(True))
        .order_by(Message.created_at.desc())
        .limit(_MAX_BOT_ROWS)
    )
    session.execute(delete(Message).where(Message.is_bot.is_(True), Message.id.not_in(keep)))
    session.commit()


def list_messages(session: Session) -> list[Message]:
    return list(
        session.scalars(
            select(Message).order_by(Message.created_at.desc()).limit(_LIST_LIMIT)
        ).all()
    )
