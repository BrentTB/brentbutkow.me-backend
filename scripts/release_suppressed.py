"""Release a recall batch the dispatcher's backfill guard held back.

Usage
-----
    RELEASE_SINCE=36h RELEASE_COUNTRIES=us python -m scripts.release_suppressed          # dry run
    RELEASE_SINCE=36h RELEASE_COUNTRIES=us RELEASE_APPLY=true python -m scripts.release_suppressed

Or from GitHub: Actions → "Release suppressed digests" (.github/workflows/release-suppressed.yml).
It has its own workflow rather than a slot in run-script.yml precisely because it mass-sends —
its window/country inputs belong to it alone, not to every ingest and backfill in that dropdown.

Why this exists
---------------
When one country's fresh batch exceeds `settings.backfill_guard_threshold`, the daily dispatcher
suppresses that country's subscriber digests, alerts the operator, and *still advances the cursor*
— so those recalls are no longer "new" and no later run will pick them up. That is deliberate: a
bulk load must not become a 137-line email (#19→#21). This script is the other half — the operator
having reviewed the held batch and judged it legitimate, it re-runs one dispatch cycle scoped to
exactly that window and those countries, with the guard off.

Safety properties (this path mass-sends, so all of them matter):
  * Dry run by default. Without RELEASE_APPLY=true it only counts and prints — no email leaves.
  * Country-scoped, and bounded at both ends. Only the named countries inside
    (RELEASE_SINCE, RELEASE_UNTIL] are considered, so a release can't re-send recalls subscribers
    already got — from a country that dispatched normally, or from a daily run since the hold.
  * The persistent cursor is never rewound or advanced (`since=` puts run_dispatch in release
    mode). The daily cycle keeps running off its own cursor, untouched by this.
  * The daily send cap and per-subscription matching still apply — this reuses run_dispatch, it
    does not re-implement the send loop.
  * Not registered in scripts/backfill_all.py, and not in run-script.yml's dropdown, on purpose:
    both are for data scripts that may be run unattended; this one sends email to real people.

RELEASE_SINCE and the optional RELEASE_UNTIL each accept an ISO-8601 instant
("2026-09-08T05:00:00Z") or a relative age ("36h", "3d"). SINCE must reach back before the held
run; set UNTIL to just after it whenever a normal daily run has happened since, or that run's
already-delivered recalls go out a second time. The dry run prints the batch — check its count
against the "Suppressed (backfill guard)" number in the operator digest before applying.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.db import SessionLocal
from app.modules.recalls.models import Recall
from app.modules.recalls.schemas import RecallCountry
from app.subscriptions.dispatcher import run_dispatch
from app.subscriptions.email import email_disabled
from app.subscriptions.matcher import recall_matches
from app.subscriptions.models import Subscription

NAME = "release_suppressed"

_RELATIVE = re.compile(r"^(\d+)([hd])$")


def parse_since(value: str) -> datetime:
    """Parse RELEASE_SINCE — "36h"/"3d" relative, or an ISO-8601 instant. Always tz-aware UTC."""
    text = value.strip()
    relative = _RELATIVE.match(text)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)
        delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        if delta <= timedelta(0):
            raise ValueError("RELEASE_SINCE must be a positive age, e.g. 36h")
        return datetime.now(UTC) - delta
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    # A naive timestamp would compare against tz-aware created_at and blow up mid-query.
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_countries(value: str) -> set[str]:
    """Parse RELEASE_COUNTRIES — a comma-separated list validated against RecallCountry."""
    countries = {item.strip().lower() for item in value.split(",") if item.strip()}
    if not countries:
        raise ValueError("RELEASE_COUNTRIES must name at least one country, e.g. 'us'")
    known = {country.value for country in RecallCountry}
    unknown = countries - known
    if unknown:
        raise ValueError(
            f"Unknown country code(s): {', '.join(sorted(unknown))}. "
            f"Known: {', '.join(sorted(known))}."
        )
    return countries


def preview(since: datetime, until: datetime | None, countries: set[str]) -> None:
    """Print what a release of this window would send, without sending anything."""
    session = SessionLocal()
    try:
        has_date = (Recall.report_date.isnot(None)) | (Recall.recall_initiation_date.isnot(None))
        window = has_date & (Recall.created_at > since) & (Recall.country.in_(sorted(countries)))
        if until is not None:
            window = window & (Recall.created_at <= until)
        recalls = list(
            session.scalars(
                select(Recall)
                .where(window)
                # Same narrow column set the dispatcher loads — never the heavy `raw` JSONB.
                .options(
                    load_only(
                        Recall.source_url,
                        Recall.product_description,
                        Recall.company_name,
                        Recall.country,
                        Recall.category,
                        Recall.severity_label,
                        Recall.entities,
                        Recall.notifying_country,
                        Recall.distribution_countries,
                    )
                )
            ).all()
        )
        subs = list(
            session.scalars(select(Subscription).where(Subscription.status == "active")).all()
        )
        by_country = Counter(recall.country for recall in recalls)
        would_receive = sum(1 for sub in subs if any(recall_matches(r, sub) for r in recalls))
        upper = until.isoformat() if until else "now"
        print(f"Window: {since.isoformat()} < created_at <= {upper}")
        print(f"Countries: {', '.join(sorted(countries))}")
        print(f"Recalls in window: {len(recalls)} ({dict(by_country) or 'none'})")
        print(f"Active subscriptions: {len(subs)}")
        print(f"Would receive a digest: {would_receive}")
        if email_disabled():
            print("NOTE: RESEND_API_KEY is not set — an apply run would send nothing.")
        print("\nDry run. Set RELEASE_APPLY=true to actually send.")
    finally:
        session.close()


def release(since: datetime, until: datetime | None, countries: set[str]) -> dict:
    """Run one release dispatch cycle: this window, these countries, guard off, cursor untouched."""
    session = SessionLocal()
    try:
        return asyncio.run(
            run_dispatch(
                session,
                since=since,
                until=until,
                only_countries=countries,
                apply_backfill_guard=False,
            )
        )
    finally:
        session.close()


def main() -> None:
    since_raw = os.environ.get("RELEASE_SINCE", "")
    until_raw = os.environ.get("RELEASE_UNTIL", "").strip()
    countries_raw = os.environ.get("RELEASE_COUNTRIES", "")
    if not since_raw or not countries_raw:
        sys.exit(
            "RELEASE_SINCE and RELEASE_COUNTRIES are both required, e.g. "
            "RELEASE_SINCE=36h RELEASE_COUNTRIES=us"
        )
    try:
        since = parse_since(since_raw)
        until = parse_since(until_raw) if until_raw else None
        countries = parse_countries(countries_raw)
    except ValueError as exc:
        sys.exit(str(exc))
    if until is not None and until <= since:
        sys.exit("RELEASE_UNTIL must be after RELEASE_SINCE — that window holds nothing.")

    if os.environ.get("RELEASE_APPLY", "").strip().lower() not in {"true", "1", "yes"}:
        preview(since, until, countries)
        return

    upper = until.isoformat() if until else "now"
    print(
        f"Releasing {', '.join(sorted(countries))} recalls created in "
        f"({since.isoformat()}, {upper}]…"
    )
    summary = release(since, until, countries)
    print(f"Release complete: {summary}")


if __name__ == "__main__":
    main()
