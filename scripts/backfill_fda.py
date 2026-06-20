from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.models import IngestRun
from app.modules.recalls.openfda import MAX_LIMIT_PER_REQUEST, MAX_SKIP
from app.modules.recalls.service import run_fda_ingest

NAME = "openFDA history seed"

# Seed as much history as openFDA's skip cap allows in one pass (~26k records).
# Run once: `python -m scripts.backfill_fda`. The daily ingest keeps it current afterward.
BACKFILL_LIMIT = MAX_SKIP + MAX_LIMIT_PER_REQUEST


def status(session: Session) -> tuple[bool, str]:
    # Only a backfill fetches more than one request's worth in a single run, so if no past openFDA
    # run exceeded the per-request cap, the full history was never seeded.
    max_fetched = (
        session.scalar(
            select(func.max(IngestRun.fetched_count)).where(IngestRun.source == "openfda_food")
        )
        or 0
    )
    if max_fetched > MAX_LIMIT_PER_REQUEST:
        return False, f"already seeded (a past run fetched {max_fetched})"
    return True, f"largest openFDA fetch is {max_fetched} (≤ {MAX_LIMIT_PER_REQUEST}/request)"


def main() -> None:
    session = SessionLocal()
    try:
        result = run_fda_ingest(session, limit=BACKFILL_LIMIT)
        print(
            f"Backfill complete: fetched {result.fetched}, "
            f"{result.new} new, upserted {result.upserted}."
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
