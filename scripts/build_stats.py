from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.models import Recall, RecallStatsCache
from app.modules.recalls.service import rebuild_stats

NAME = "stats"


def status(session: Session) -> tuple[bool, str]:
    # The /recalls/stats payload aggregates the whole recalls corpus, so it goes stale whenever a
    # recall changes — a new ingest, but also a standalone reclassify or a severity/entity backfill
    # that rewrites the columns the aggregates, anomalies, and forecast are built from. Every such
    # write bumps Recall.updated_at (onupdate), so compare the newest row to the last build: a row
    # updated after computed_at means the materialized payload lags the data.
    built_at = session.scalar(select(func.max(RecallStatsCache.computed_at)))
    if built_at is None:
        return True, "stats not materialized yet"
    last_change = session.scalar(select(func.max(Recall.updated_at)))
    if last_change is not None and last_change > built_at:
        return True, "recalls changed since the last stats build"
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
