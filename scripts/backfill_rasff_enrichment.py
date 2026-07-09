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

from sqlalchemy import or_, select
from sqlalchemy.orm import defer

from app.db import SessionLocal
from app.modules.recalls.models import Recall
from app.modules.recalls.rasff_eu import RasffRecord, _status, enrich_records

# Rows still on the RASFF Window fallback (or with no URL) haven't had a national link attached yet.
_FALLBACK_HOST = "webgate.ec.europa.eu"

# Enrich in batches so a long pass commits incrementally, and pause between SPA calls to stay polite
# to the undocumented endpoint over the full history.
_BATCH = 200
_DELAY_SECONDS = 0.3


def main() -> None:
    session = SessionLocal()
    try:
        rows = list(
            session.scalars(
                select(Recall)
                .options(defer(Recall.raw))
                .where(
                    Recall.source == "rasff_eu",
                    Recall.event_id.is_not(None),
                    or_(
                        Recall.source_url.is_(None),
                        Recall.source_url.contains(_FALLBACK_HOST),
                    ),
                )
                .order_by(Recall.recall_number)
            )
        )
        print(f"Enriching {len(rows)} RASFF recalls lacking a national-authority link…")

        upgraded = 0
        for start in range(0, len(rows), _BATCH):
            batch = rows[start : start + _BATCH]
            # event_id holds the NOTIF_ID the detail endpoint is keyed on (guarded non-None above).
            records = [
                RasffRecord(NOTIF_ID=int(row.event_id), NOTIFICATION_REFERENCE=row.recall_number)
                for row in batch
                if row.event_id is not None
            ]
            enrich_records(records, delay=_DELAY_SECONDS)
            by_ref = {r.reference: r for r in records}
            for row in batch:
                record = by_ref.get(row.recall_number)
                if record is None:
                    continue
                # Only upgrade when a national link was found — never clobber the fallback.
                if record.enriched_url:
                    row.source_url = record.enriched_url
                    upgraded += 1
                status = _status(record)
                if status:
                    row.status = status
            session.commit()
            print(f"  {min(start + _BATCH, len(rows))}/{len(rows)}… ({upgraded} upgraded so far)")

        print(f"Done: {upgraded}/{len(rows)} recalls now carry a national-authority link.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
