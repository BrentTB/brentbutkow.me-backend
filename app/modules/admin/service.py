from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.admin.schemas import (
    AdminOverview,
    IngestSummary,
    MessageCounts,
    NullspaceCounts,
    RecallCounts,
    SubscriptionCounts,
)
from app.modules.contact.models import Message
from app.modules.nullspace.models import Score
from app.modules.recalls.models import IngestRun, Recall
from app.subscriptions.models import Subscription


def _message_counts(session: Session) -> MessageCounts:
    total = session.scalar(select(func.count()).select_from(Message)) or 0
    bot = (
        session.scalar(select(func.count()).select_from(Message).where(Message.is_bot.is_(True)))
        or 0
    )
    return MessageCounts(total=total, real=total - bot, bot=bot)


def _subscription_counts(session: Session) -> SubscriptionCounts:
    rows = session.execute(
        select(Subscription.status, func.count()).group_by(Subscription.status)
    ).all()
    by_status = {status: count for status, count in rows}
    return SubscriptionCounts(
        total=sum(by_status.values()),
        active=by_status.get("active", 0),
        pending_confirmation=by_status.get("pending_confirmation", 0),
        paused=by_status.get("paused", 0),
        unsubscribed=by_status.get("unsubscribed", 0),
    )


def _last_ingest(session: Session) -> IngestSummary | None:
    run = session.scalars(
        select(IngestRun).order_by(IngestRun.started_at.desc(), IngestRun.id.desc()).limit(1)
    ).first()
    if run is None:
        return None
    return IngestSummary(
        last_run_at=run.finished_at or run.started_at,
        status=run.status,
        fetched_count=run.fetched_count,
        upserted_count=run.upserted_count,
    )


def _recall_counts(session: Session) -> RecallCounts:
    rows = session.execute(select(Recall.country, func.count()).group_by(Recall.country)).all()
    by_country = {country: count for country, count in rows}
    return RecallCounts(
        total=sum(by_country.values()),
        us=by_country.get("us", 0),
        uk=by_country.get("uk", 0),
        za=by_country.get("za", 0),
    )


def _nullspace_counts(session: Session) -> NullspaceCounts:
    total = session.scalar(select(func.count()).select_from(Score)) or 0
    flagged = (
        session.scalar(select(func.count()).select_from(Score).where(Score.flagged.is_(True))) or 0
    )
    return NullspaceCounts(total=total, legit=total - flagged, flagged=flagged)


def build_overview(session: Session) -> AdminOverview:
    return AdminOverview(
        messages=_message_counts(session),
        subscriptions=_subscription_counts(session),
        ingest=_last_ingest(session),
        recalls=_recall_counts(session),
        nullspace=_nullspace_counts(session),
    )


def list_messages(
    session: Session, *, limit: int, offset: int, include_bots: bool
) -> tuple[list[Message], int]:
    base = select(Message)
    count_q = select(func.count()).select_from(Message)
    if not include_bots:
        base = base.where(Message.is_bot.is_(False))
        count_q = count_q.where(Message.is_bot.is_(False))
    total = session.scalar(count_q) or 0
    items = list(
        session.scalars(
            base.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit).offset(offset)
        ).all()
    )
    return items, total


def list_subscriptions(
    session: Session, *, limit: int, offset: int, status: str | None
) -> tuple[list[Subscription], int]:
    base = select(Subscription)
    count_q = select(func.count()).select_from(Subscription)
    if status is not None:
        base = base.where(Subscription.status == status)
        count_q = count_q.where(Subscription.status == status)
    total = session.scalar(count_q) or 0
    items = list(
        session.scalars(
            base.order_by(Subscription.created_at.desc()).limit(limit).offset(offset)
        ).all()
    )
    return items, total


def list_scores(
    session: Session, *, limit: int, offset: int, flagged: bool | None
) -> tuple[list[Score], int]:
    # flagged is tri-state: None → all runs, True → only flagged (the rejected-from-leaderboard
    # ones worth inspecting), False → only the legit scores.
    base = select(Score)
    count_q = select(func.count()).select_from(Score)
    if flagged is not None:
        base = base.where(Score.flagged.is_(flagged))
        count_q = count_q.where(Score.flagged.is_(flagged))
    total = session.scalar(count_q) or 0
    items = list(
        session.scalars(
            base.order_by(Score.created_at.desc(), Score.id.desc()).limit(limit).offset(offset)
        ).all()
    )
    return items, total
