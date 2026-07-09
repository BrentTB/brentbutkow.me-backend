"""One-off: seed the full public RASFF history (2020+) from the official DG SANTE API.

Pulls every recall-classified RASFF notification (no date window) and upserts it, WITHOUT the
best-effort SPA enrichment — that would mean ~31k detail calls in one run. Run
scripts/backfill_rasff_enrichment.py afterwards to attach the national-authority links. Idempotent:
re-running just re-upserts. After it, the daily scripts/ingest_rasff.py keeps the corpus current.
"""

from app.db import SessionLocal
from app.modules.recalls.service import run_rasff_ingest


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
