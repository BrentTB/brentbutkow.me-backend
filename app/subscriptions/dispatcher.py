"""
app/subscriptions/dispatcher.py
One dispatch cycle: match new recalls to active subscribers and send digest emails.

Usage
-----
    from app.subscriptions.dispatcher import run_dispatch

    # Called by the ingest pipeline or a scheduled job after new recalls are persisted.
    summary = await run_dispatch(db_session)

Module-level cursor
-------------------
_last_dispatch_run_at is set to datetime.now(UTC) at the end of every successful cycle.
On the very first run (None), all recalls with a non-null report_date or
recall_initiation_date are treated as "new".
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.recalls.models import Recall
from app.subscriptions.email import (
    send_digest_email,
    send_operator_digest_email,
    send_with_retry,
)
from app.subscriptions.matcher import recall_is_new, recall_matches
from app.subscriptions.models import Subscription

logger = logging.getLogger(__name__)

# Module-level cursor — persists across calls within a single process lifetime.
_last_dispatch_run_at: datetime | None = None

# Daily send cap per requirement 5.9 / 5.10
_DAILY_SEND_CAP = 89


async def run_dispatch(db_session: Session) -> dict:
    """Run one dispatch cycle. Returns a summary metrics dict."""
    global _last_dispatch_run_at  # noqa: PLW0603

    # ------------------------------------------------------------------
    # 1. Load new recalls
    # ------------------------------------------------------------------
    if _last_dispatch_run_at is None:
        # First run — treat all recalls that have at least one date as "new"
        stmt_recalls = select(Recall).where(
            (Recall.report_date.isnot(None)) | (Recall.recall_initiation_date.isnot(None))
        )
    else:
        stmt_recalls = select(Recall).where(Recall.created_at > _last_dispatch_run_at)

    new_recalls: list[Recall] = list(db_session.scalars(stmt_recalls).all())

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

    # will_receive_count: number of active subs with at least one matching new recall
    will_receive_count = 0
    for sub in active_subs:
        for recall in new_recalls:
            if recall_matches(recall, sub) and recall_is_new(recall, sub):
                will_receive_count += 1
                break

    metrics = {
        "new_recall_count": len(new_recalls),
        "total_active": len(active_subs),
        "will_receive_count": will_receive_count,
        "skipped_count": 0,  # updated during the send loop
        "stale_pending_count": stale_pending_count_row,
        "oldest_last_digest_at": oldest_last_digest_at,
    }

    # ------------------------------------------------------------------
    # 4. Send operator digest email first (req 9.1–9.2)
    # ------------------------------------------------------------------
    operator_email = os.getenv("OPERATOR_EMAIL", "").strip()
    if not operator_email:
        logger.warning("OPERATOR_EMAIL is not set — operator digest will not be sent.")
    else:
        try:
            await asyncio.to_thread(
                send_operator_digest_email,
                metrics,
                new_recalls,
                [],  # errors list — populated later; send an empty list now
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

    for sub in active_subs:
        # 5a. Collect matching new recalls for this subscription
        matching_recalls = [
            r for r in new_recalls if recall_matches(r, sub) and recall_is_new(r, sub)
        ]

        # 5b. Skip if zero matches
        if not matching_recalls:
            continue

        # 5c. Daily cap check
        if sent_count >= _DAILY_SEND_CAP:
            skipped_cap_count += 1
            # Append today's ISO date to skipped_at (avoid duplicates)
            if today_iso not in sub.skipped_at:
                sub.skipped_at = list(sub.skipped_at) + [today_iso]
            db_session.commit()

            if not cap_warning_logged:
                logger.warning(
                    "Daily send cap of %d reached — remaining subscriptions will be skipped.",
                    _DAILY_SEND_CAP,
                )
                cap_warning_logged = True
            continue

        # 5d. Send digest with retry
        async def _send(s=sub, r=matching_recalls):  # default-arg capture
            return await asyncio.to_thread(send_digest_email, s, r)

        try:
            await send_with_retry(_send)
            # Success
            sub.last_digest_at = datetime.now(UTC)
            sub.skipped_at = []
            db_session.commit()
            sent_count += 1
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if 400 <= status_code < 500:
                # Permanent 4xx — unsubscribe
                sub.status = "unsubscribed"
                db_session.commit()
                logger.error(
                    "Permanent delivery failure for subscription %s (HTTP %d): %s",
                    sub.id,
                    status_code,
                    exc,
                )
            else:
                # Transient exhausted — do not update last_digest_at
                logger.error(
                    "Transient delivery failure exhausted for subscription %s: %s",
                    sub.id,
                    exc,
                )
            error_count += 1
        except Exception as exc:
            # Any other exception after send_with_retry raises
            logger.error(
                "Delivery failure for subscription %s: %s",
                sub.id,
                exc,
            )
            error_count += 1

    # Update skipped_count in metrics (now we know the real value)
    metrics["skipped_count"] = skipped_cap_count

    # ------------------------------------------------------------------
    # 6. Advance module-level cursor
    # ------------------------------------------------------------------
    _last_dispatch_run_at = datetime.now(UTC)

    # ------------------------------------------------------------------
    # 7. Summary log
    # ------------------------------------------------------------------
    logger.info(
        "Dispatch complete — new_recalls=%d active_subs=%d sent=%d skipped_cap=%d errors=%d",
        len(new_recalls),
        len(active_subs),
        sent_count,
        skipped_cap_count,
        error_count,
    )

    return {
        "new_recalls": len(new_recalls),
        "active_subs": len(active_subs),
        "sent": sent_count,
        "skipped_cap": skipped_cap_count,
        "errors": error_count,
    }
