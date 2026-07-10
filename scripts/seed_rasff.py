"""One-off: seed the full public RASFF history (2020+) from the official DG SANTE API.

Pulls every recall-classified RASFF notification (no date window) and upserts it, WITHOUT the
best-effort SPA enrichment — that would mean ~31k detail calls in one run. Run
scripts/backfill_rasff_enrichment.py afterwards to attach the national-authority links. Idempotent:
re-running just re-upserts. After it, the daily scripts/ingest_rasff.py keeps the corpus current.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.models import IngestRun
from app.modules.recalls.service import run_rasff_ingest

NAME = "EU RASFF history seed"

# Row count can't tell "seeded" from "a daily ingest ran once": the daily job's rolling 14-day
# window creates a few hundred rows that would falsely read as seeded and suppress the full pull.
# Fetch size can — only this seed (days=None) pulls the whole 2020+ corpus in one run (~22k
# recall-classified notifications), while a windowed run stays far smaller (14 days ≈ 170, a full
# manual year ≈ 4.5k). This floor sits above any plausible windowed catch-up yet well below the
# full history. Mirrors scripts/backfill_fda.py, which separates its seed from daily runs the same
# way.
_SEED_FETCH_FLOOR = 10_000


def status(session: Session) -> tuple[bool, str]:
    max_fetched = (
        session.scalar(
            select(func.max(IngestRun.fetched_count)).where(IngestRun.source == "rasff_eu")
        )
        or 0
    )
    if max_fetched >= _SEED_FETCH_FLOOR:
        return False, f"already seeded (a past run fetched {max_fetched})"
    return (
        True,
        f"largest RASFF fetch is {max_fetched} (< {_SEED_FETCH_FLOOR}) — history not seeded",
    )


def main() -> None:
    session = SessionLocal()
    try:
        result = run_rasff_ingest(session, days=None, enrich=False)
        print(
            f"RASFF (EU) history seed complete: fetched {result.fetched}, "
            f"{result.new} new, upserted {result.upserted}. "
            "Run scripts/backfill_rasff_enrichment.py next to attach national-authority links."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
