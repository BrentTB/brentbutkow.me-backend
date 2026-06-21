from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.models import IngestRun, RecallStatsCache
from app.modules.recalls.service import rebuild_stats

NAME = "stats"


def status(session: Session) -> tuple[bool, str]:
    # The /recalls/stats payload is materialized per country and refreshed at ingest, not per row.
    # Stale when never built, or when a successful ingest landed after the last build — new recalls
    # would change the aggregates, anomalies, and forecast the row holds.
    built_at = session.scalar(select(func.max(RecallStatsCache.computed_at)))
    if built_at is None:
        return True, "stats not materialized yet"
    last_ok_ingest = session.scalar(
        select(func.max(IngestRun.finished_at)).where(IngestRun.status == "ok")
    )
    if last_ok_ingest is not None and last_ok_ingest > built_at:
        return True, "recalls ingested since the last stats build"
    return False, "stats materialized"


# Materializes the /recalls/stats payload per country into recall_stats, so the request path reads a
# row instead of recomputing the aggregations + anomaly scan + forecast. Independent of analytics /
# events (it only reads `recalls`). Run after the ingests: `python -m scripts.build_stats`.
def main() -> None:
    session = SessionLocal()
    try:
        summary = rebuild_stats(session)
        print(f"Rebuilt stats: materialized {summary['countries']} country payloads.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
