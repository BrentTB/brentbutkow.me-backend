"""One-off: attach national food-authority links to already-seeded RASFF recalls.

The history seed (scripts/seed_rasff.py) stores each recall with the deterministic RASFF Window
page as its source_url. This pass walks those rows and, for each, fetches the RASFF Window detail
(the only place the per-notification `measures` live) to recover the national-authority URL and the
action taken — the enrichment the daily ingest does inline but the seed skips.

Best-effort and throttled: a row whose detail fetch fails or has no national link keeps its RASFF
Window fallback. "Already enriched" = source_url no longer points at webgate (a national host), so
those are skipped; re-running only retries rows that still lack a national link. Targets only rows
that carry a NOTIF_ID (stored in event_id), which the detail endpoint is keyed on.
"""

import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.models import Recall
from app.modules.recalls.rasff_eu import RASFF_WINDOW_HOST, RasffRecord, _status, enrich_records
from app.modules.recalls.schemas import RecallSource
from app.modules.recalls.service import rasff_recalls_needing_enrichment

NAME = "RASFF national-link enrichment"


def status(session: Session) -> tuple[bool, str]:
    # "Never ran" is the only state this can detect reliably: after any completed pass, roughly a
    # fifth of rows carry a national link and the rest legitimately have none at source — so
    # rows-still-on-fallback can NOT mean "due" (it would re-run a ~25-minute network pass forever).
    # Re-running to retry the remainder (e.g. after the harvest learns a new payload location)
    # stays a deliberate manual run.
    total = (
        session.scalar(select(func.count()).where(Recall.source == RecallSource.rasff.value)) or 0
    )
    if not total:
        return False, "no EU RASFF rows to enrich (history seed runs first)"
    national = (
        session.scalar(
            select(func.count()).where(
                Recall.source == RecallSource.rasff.value,
                Recall.source_url.is_not(None),
                ~Recall.source_url.contains(RASFF_WINDOW_HOST),
            )
        )
        or 0
    )
    if national == 0:
        return True, f"none of {total} EU rows carry a national-authority link — never enriched"
    return False, f"{national}/{total} EU rows carry a national link (re-run manually to retry)"


# Enrich in batches so a long pass commits incrementally. The SPA detail endpoint costs ~1s per
# call regardless of outcome, so throughput comes from bounded concurrency (see enrich_records) —
# 12 workers puts the full ~22k-row history around half an hour instead of the ~8 hours a
# sequential pass with pacing sleeps took. Twelve in-flight requests is still a light load for the
# Commission's infrastructure, but don't push further on an undocumented endpoint.
_BATCH = 400
_WORKERS = 12


def main() -> None:
    session = SessionLocal()
    try:
        # The work-list query lives in service.py (the DB layer) so it's covered by a DB-free test
        # asserting it filters on the same source value normalize_rasff writes — the mismatch that
        # made an earlier version match zero rows.
        rows = rasff_recalls_needing_enrichment(session)
        print(f"Enriching {len(rows)} RASFF recalls lacking a national-authority link…")

        started = time.monotonic()
        upgraded = 0
        for start in range(0, len(rows), _BATCH):
            batch = rows[start : start + _BATCH]
            # event_id holds the NOTIF_ID the detail endpoint is keyed on (guarded non-None above).
            records = [
                RasffRecord(NOTIF_ID=int(row.event_id), NOTIFICATION_REFERENCE=row.recall_number)
                for row in batch
                if row.event_id is not None
            ]
            enrich_records(records, workers=_WORKERS)
            by_ref = {r.reference: r for r in records}
            for row in batch:
                record = by_ref.get(row.recall_number)
                if record is None:
                    continue
                # Only upgrade when a national link was found — never clobber the fallback.
                if record.enriched_url:
                    row.source_url = record.enriched_url
                    upgraded += 1
                actions_taken = _status(record)
                if actions_taken:
                    row.status = actions_taken
            session.commit()
            done = min(start + _BATCH, len(rows))
            rate = done / (time.monotonic() - started)
            eta_min = (len(rows) - done) / rate / 60 if rate else 0
            print(
                f"  {done}/{len(rows)}… ({upgraded} upgraded, "
                f"{rate:.1f} rows/s, ~{eta_min:.0f} min left)"
            )

        print(f"Done: {upgraded}/{len(rows)} recalls now carry a national-authority link.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
