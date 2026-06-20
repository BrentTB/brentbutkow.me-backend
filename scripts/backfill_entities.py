from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.models import Recall

NAME = "entities"

# Recompute-dependency map (what to re-run when an input changes): scripts/backfill_all.py
_BATCH = 1000

# How many empty-entity rows to re-extract when deciding whether the backfill is still due (status).
_PROBE_SAMPLE = 500


def status(session: Session) -> tuple[bool, str]:
    # An empty entities array is ambiguous: the recall may genuinely name nothing, or extraction
    # never ran for it (rows seeded before the feature, or before a gazetteer change) — a plain
    # empty-count can't tell those apart. So probe: re-extract a sample of the empty rows; if any
    # would gain entities, those rows are stale and the backfill is due. (Read-only re-extraction.)
    sample = session.scalars(
        select(Recall.reason_text)
        .where(func.jsonb_array_length(Recall.entities) == 0)
        .limit(_PROBE_SAMPLE)
    ).all()
    stale = sum(1 for reason_text in sample if extract_entities(reason_text))
    if stale:
        return True, f"{stale} of {len(sample)} sampled empty rows would gain entities"
    return False, f"{len(sample)} sampled empty rows checked; none would gain entities"


# One-time (re-runnable) pass: extract entities from every stored recall's reason_text. New rows
# get entities at ingest via the normalizers — this seeds the existing backfill.
def main() -> None:
    # SessionLocal's expire_on_commit=False default keeps loaded rows usable across batch commits.
    session = SessionLocal()
    try:
        recalls = session.scalars(select(Recall)).all()
        for index, recall in enumerate(recalls, start=1):
            recall.entities = extract_entities(recall.reason_text)
            if index % _BATCH == 0:
                session.commit()
                print(f"  {index}/{len(recalls)}…")
        session.commit()
        print(f"Backfilled entities for {len(recalls)} recalls.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
