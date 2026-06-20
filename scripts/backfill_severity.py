from sqlalchemy import select

from app.db import SessionLocal
from app.modules.recalls.models import Recall
from app.modules.recalls.severity import score_severity

_BATCH = 1000


# One-time (re-runnable) pass: compute severity_score + severity_label for every stored recall from
# fields already on the row. New rows get severity at ingest via the normalizers — this seeds the
# existing rows the migration left at the 0 / 'low' server default.
# Run: `python -m scripts.backfill_severity`.
def main() -> None:
    # SessionLocal's expire_on_commit=False default keeps loaded rows usable across batch commits.
    session = SessionLocal()
    try:
        recalls = session.scalars(select(Recall)).all()
        for index, recall in enumerate(recalls, start=1):
            score, label = score_severity(
                classification=recall.classification,
                category=recall.category,
                entities=recall.entities,
                states=recall.states,
                distribution_pattern=recall.distribution_pattern,
            )
            recall.severity_score = score
            recall.severity_label = label
            if index % _BATCH == 0:
                session.commit()
                print(f"  {index}/{len(recalls)}…")
        session.commit()
        print(f"Backfilled severity for {len(recalls)} recalls.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
