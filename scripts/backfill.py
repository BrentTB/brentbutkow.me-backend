from app.db import SessionLocal
from app.modules.recalls.openfda import MAX_LIMIT_PER_REQUEST, MAX_SKIP
from app.modules.recalls.service import run_ingest

# Seed as much history as openFDA's skip cap allows in one pass (~26k records).
# Run once: `python -m scripts.backfill`. The daily ingest keeps it current afterward.
BACKFILL_LIMIT = MAX_SKIP + MAX_LIMIT_PER_REQUEST


def main() -> None:
    session = SessionLocal()
    try:
        result = run_ingest(session, limit=BACKFILL_LIMIT)
        print(f"Backfill complete: fetched {result.fetched}, upserted {result.upserted}.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
