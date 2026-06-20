from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.models import Recall

NAME = "entities"

_BATCH = 1000


def status(session: Session) -> tuple[bool, str]:
    # An empty array is a legitimate result — most recalls name no allergen/pathogen/hazard — so a
    # partial empty count is expected, not a backlog. Only "every row empty" means it never ran;
    # once it has, new rows self-populate at ingest and re-running won't shrink the count.
    total = session.scalar(select(func.count()).select_from(Recall)) or 0
    empty = (
        session.scalar(
            select(func.count())
            .select_from(Recall)
            .where(func.jsonb_array_length(Recall.entities) == 0)
        )
        or 0
    )
    if total and empty == total:
        return True, "no row has any extracted entities yet"
    return False, f"extraction has run ({empty}/{total} rows legitimately have none)"


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
