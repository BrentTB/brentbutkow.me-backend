from sqlalchemy import select

from app.db import SessionLocal
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.models import Recall

_BATCH = 1000


# One-time (re-runnable) pass: extract entities from every stored recall's reason_text. New rows
# get entities at ingest via the normalizers — this seeds the existing backfill.
def main() -> None:
    session = SessionLocal()
    session.expire_on_commit = False  # keep loaded rows usable across batch commits
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
