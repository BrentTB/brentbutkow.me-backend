"""
app/subscriptions/dispatcher.py
One dispatch cycle: match new recalls to active subscribers and send digest emails.

Usage
-----
    from app.subscriptions.dispatcher import run_dispatch

    # Called by the ingest pipeline or a scheduled job after new recalls are persisted.
    summary = await run_dispatch(db_session)

Dispatch cursor
---------------
A recall is "new" if it was ingested since the last run. DispatchState.last_run_at is persisted to
the DB at the end of every cycle, so a restart or deploy doesn't re-treat the whole backlog as new.
On the very first run (null cursor), a 1-day window stands in, so a subscriber's first digest is the
last day's new recalls rather than a backlog.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from resend.exceptions import ResendError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only

from app.config import settings
from app.modules.contact.models import Message
from app.modules.recalls.models import Recall
from app.subscriptions.email import (
    email_disabled,
    is_permanent_failure,
    send_digest_email,
    send_operator_digest_email,
    send_with_retry,
    status_code,
)
from app.subscriptions.matcher import recall_matches
from app.subscriptions.models import DispatchState, Subscription

logger = logging.getLogger(__name__)

# Free-tier daily send cap — the operator digest takes the 90th slot, leaving 89 for subscribers.
_DAILY_SEND_CAP = 89

# Only the columns the matcher and email templates touch — never the heavy `raw` JSONB (or the
# deferred search_vector). Dates aren't read off the row (they're only used in the WHERE filter).
_RECALL_COLUMNS = (
    Recall.source_url,
    Recall.product_description,
    Recall.company_name,
    Recall.country,
    Recall.category,
    Recall.severity_label,
    Recall.entities,
)

# "New" means "ingested since the last run" (the cursor below). On the first run there's no cursor,
# so fall back to a 1-day window — a subscriber's first digest is the last day's new recalls, never
# a backlog. The ingest and dispatch run back-to-back in the same job, so a day comfortably covers
# the batch just ingested.
_FIRST_RUN_LOOKBACK = timedelta(days=1)

# Backfill circuit breaker. Newness keys off `created_at` (ingest time), so a one-shot backfill,
# new source, or re-sync that fresh-inserts a big batch would otherwise blast every active
# subscriber at once. A normal daily delta is a handful of recalls; a run this far above that is a
# bulk load, not a news day. When tripped we suppress all subscriber sends, alert the operator, and
# still advance the cursor so the next run returns to normal — the operator decides what to release.
_BACKFILL_GUARD_THRESHOLD = 50


def _load_dispatch_state(db_session: Session) -> DispatchState:
    """Fetch the singleton dispatch_state row (id=1), creating it on first ever run."""
    state = db_session.get(DispatchState, 1)
    if state is None:
        state = DispatchState(id=1, last_run_at=None)
        db_session.add(state)
        db_session.flush()
    return state


def _commit_or_rollback(db_session: Session, context: str) -> bool:
    """Commit; on failure roll back so a broken session doesn't cascade into the next subscriber.

    Returns True if the commit succeeded.
    """
    try:
        db_session.commit()
        return True
    except Exception as exc:
        db_session.rollback()
        logger.error("Commit failed (%s) — rolled back: %s", context, exc)
        return False


async def run_dispatch(db_session: Session) -> dict:
    """Run one dispatch cycle. Returns a summary metrics dict."""
    if email_disabled():
        # No API key → sending is a no-op. Bail before touching subscription state so we don't
        # mark anyone as "sent" or advance the cursor over recalls that were never delivered.
        logger.warning("Email disabled (no resend_api_key) — dispatch skipped, no state changed.")
        return {
            "newRecalls": 0,
            "activeSubs": 0,
            "sent": 0,
            "skippedCap": 0,
            "errors": 0,
            "emailDisabled": True,
        }

    state = _load_dispatch_state(db_session)
    last_run_at = state.last_run_at

    # ------------------------------------------------------------------
    # 1. Load new recalls
    # ------------------------------------------------------------------
    # Require at least one date — a recall with neither is structurally invalid and shouldn't alert.
    has_date = (Recall.report_date.isnot(None)) | (Recall.recall_initiation_date.isnot(None))
    if last_run_at is None:
        cutoff = datetime.now(UTC) - _FIRST_RUN_LOOKBACK
        stmt_recalls = select(Recall).where(has_date & (Recall.created_at > cutoff))
    else:
        stmt_recalls = select(Recall).where(has_date & (Recall.created_at > last_run_at))

    # load_only keeps each row to the handful of fields we use — no `raw` JSONB — so a big backlog
    # doesn't exhaust memory.
    stmt_recalls = stmt_recalls.options(load_only(*_RECALL_COLUMNS))
    new_recalls: list[Recall] = list(db_session.scalars(stmt_recalls).all())

    # ------------------------------------------------------------------
    # 1b. Load new contact-form messages since the last run
    # ------------------------------------------------------------------
    # Folded into the operator digest rather than emailed per-message, so a burst of submissions
    # can't spam the inbox or burn the free-tier send budget. Spam (is_bot) is excluded here — the
    # admin console still surfaces it. Same cursor window as recalls.
    msg_cutoff = (datetime.now(UTC) - _FIRST_RUN_LOOKBACK) if last_run_at is None else last_run_at
    stmt_messages = (
        select(Message)
        .where(Message.is_bot.is_(False), Message.created_at > msg_cutoff)
        .order_by(Message.created_at.asc())
    )
    new_messages: list[Message] = list(db_session.scalars(stmt_messages).all())

    # Backfill guard: an abnormally large batch is a bulk load, not a news day. Trip the breaker so
    # the send loop below is a no-op — the operator still gets a digest (flagged) and the cursor
    # still advances at the end, so the next run returns to normal.
    backfill_guard_tripped = len(new_recalls) > _BACKFILL_GUARD_THRESHOLD
    if backfill_guard_tripped:
        logger.warning(
            "Backfill guard tripped — %d new recalls exceeds threshold %d. Suppressing all "
            "subscriber sends this run; operator digest still sent and cursor still advanced.",
            len(new_recalls),
            _BACKFILL_GUARD_THRESHOLD,
        )

    # ------------------------------------------------------------------
    # 2. Load all active subscriptions ordered by confirmed_at ASC
    # ------------------------------------------------------------------
    stmt_subs = (
        select(Subscription)
        .where(Subscription.status == "active")
        .order_by(Subscription.confirmed_at.asc())
    )
    active_subs: list[Subscription] = list(db_session.scalars(stmt_subs).all())

    # ------------------------------------------------------------------
    # 3. Pre-compute metrics for operator digest
    # ------------------------------------------------------------------
    stale_cutoff = datetime.now(UTC) - timedelta(hours=72)
    stale_pending_count_row = db_session.execute(
        select(func.count(Subscription.id)).where(
            Subscription.status == "pending_confirmation",
            Subscription.created_at < stale_cutoff,
        )
    ).scalar_one()

    oldest_last_digest_at = db_session.execute(
        select(func.min(Subscription.last_digest_at)).where(
            Subscription.status == "active",
            Subscription.last_digest_at.isnot(None),
        )
    ).scalar_one()

    # Match each subscription against the new recalls once, then reuse for both the metric and the
    # send loop (the matcher is the per-run hot path — O(subs × recalls)).
    # When the backfill guard is tripped, leave sub_matches empty so the send loop is a no-op.
    sub_matches: list[tuple[Subscription, list[Recall]]] = (
        []
        if backfill_guard_tripped
        else [(sub, [r for r in new_recalls if recall_matches(r, sub)]) for sub in active_subs]
    )
    will_receive_count = sum(1 for _, matches in sub_matches if matches)

    metrics = {
        "new_recall_count": len(new_recalls),
        "new_message_count": len(new_messages),
        "total_active": len(active_subs),
        "will_receive_count": will_receive_count,
        "skipped_count": 0,  # updated during the send loop
        "stale_pending_count": stale_pending_count_row,
        "oldest_last_digest_at": oldest_last_digest_at,
        "backfill_guard_tripped": backfill_guard_tripped,
    }

    # ------------------------------------------------------------------
    # 4. Send operator digest email first
    # ------------------------------------------------------------------
    # Surface a tripped guard in the operator email's errors section so it can't be missed.
    operator_errors = (
        [
            f"Backfill guard tripped: {len(new_recalls)} new recalls exceeds threshold "
            f"{_BACKFILL_GUARD_THRESHOLD}. All subscriber digests were suppressed this run — "
            f"review the batch and dispatch manually if it is legitimate.",
        ]
        if backfill_guard_tripped
        else []
    )
    operator_email = (settings.operator_email or "").strip()
    if not operator_email:
        logger.warning("operator_email is not set — operator digest will not be sent.")
    else:
        try:
            await asyncio.to_thread(
                send_operator_digest_email,
                metrics,
                new_recalls,
                operator_errors,
                new_messages,
            )
        except Exception as exc:
            logger.error("Failed to send operator digest email: %s", exc)

    # ------------------------------------------------------------------
    # 5. Per-subscription dispatch loop
    # ------------------------------------------------------------------
    today_iso = datetime.now(UTC).date().isoformat()
    sent_count = 0
    error_count = 0
    skipped_cap_count = 0
    cap_warning_logged = False

    for sub, matching_recalls in sub_matches:
        # 5a. Skip if zero matches
        if not matching_recalls:
            continue

        # 5b. Daily cap check
        if sent_count >= _DAILY_SEND_CAP:
            skipped_cap_count += 1
            # Append today's ISO date to skipped_at (avoid duplicates)
            if today_iso not in sub.skipped_at:
                sub.skipped_at = list(sub.skipped_at) + [today_iso]
            _commit_or_rollback(db_session, f"cap-skip for {sub.id}")

            if not cap_warning_logged:
                logger.warning(
                    "Daily send cap of %d reached — remaining subscriptions will be skipped.",
                    _DAILY_SEND_CAP,
                )
                cap_warning_logged = True
            continue

        # 5c. Send digest with retry
        async def _send(s=sub, r=matching_recalls):  # default-arg capture
            return await asyncio.to_thread(send_digest_email, s, r)

        try:
            await send_with_retry(_send)
        except ResendError as exc:
            if is_permanent_failure(exc):
                # Permanent client error (e.g. invalid/blocked recipient) — stop emailing them
                # so we don't keep hitting a dead address and harming sender reputation.
                sub.status = "unsubscribed"
                _commit_or_rollback(db_session, f"unsubscribe {sub.id}")
                logger.error(
                    "Permanent delivery failure for subscription %s (HTTP %s) — unsubscribed: %s",
                    sub.id,
                    status_code(exc),
                    exc,
                )
            else:
                # Transient failure survived all retries — leave last_digest_at so the next
                # run retries this subscriber.
                logger.error(
                    "Transient delivery failure exhausted for subscription %s: %s",
                    sub.id,
                    exc,
                )
            error_count += 1
        except Exception as exc:
            # Unexpected non-Resend error — log and move on to the next subscriber.
            logger.error(
                "Delivery failure for subscription %s: %s",
                sub.id,
                exc,
            )
            error_count += 1
        else:
            # Delivered — advance the digest cursor. If the commit fails, the rollback reverts it,
            # so count it as an error rather than a phantom success.
            sub.last_digest_at = datetime.now(UTC)
            sub.skipped_at = []
            if _commit_or_rollback(db_session, f"digest for {sub.id}"):
                sent_count += 1
            else:
                error_count += 1

    # Update skipped_count in metrics (now we know the real value)
    metrics["skipped_count"] = skipped_cap_count

    # ------------------------------------------------------------------
    # 6. Advance the persisted dispatch cursor
    # ------------------------------------------------------------------
    state.last_run_at = datetime.now(UTC)
    _commit_or_rollback(db_session, "advance dispatch cursor")

    # ------------------------------------------------------------------
    # 7. Summary log
    # ------------------------------------------------------------------
    logger.info(
        "Dispatch complete — new_recalls=%d active_subs=%d sent=%d skipped_cap=%d errors=%d "
        "backfill_guard=%s",
        len(new_recalls),
        len(active_subs),
        sent_count,
        skipped_cap_count,
        error_count,
        backfill_guard_tripped,
    )

    return {
        "newRecalls": len(new_recalls),
        "activeSubs": len(active_subs),
        "sent": sent_count,
        "skippedCap": skipped_cap_count,
        "errors": error_count,
        "backfillGuardTripped": backfill_guard_tripped,
    }
