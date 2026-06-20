from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.models import Recall
from app.modules.recalls.severity import score_severity

NAME = "severity"

_BATCH = 1000


def status(session: Session) -> tuple[bool, str]:
    # Real scores are ≥ 22 (severity.py), so score == 0 is exactly the migration's server default —
    # a pre-severity row this pass hasn't touched yet.
    pending = (
        session.scalar(select(func.count()).select_from(Recall).where(Recall.severity_score == 0))
        or 0
    )
    if pending:
        return True, f"{pending} row(s) still at the score=0 migration default"
    return False, "all rows have a real severity score"


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
