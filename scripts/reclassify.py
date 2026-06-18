from sqlalchemy import select

from app.db import SessionLocal
from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.models import Recall


# Re-derives category + confidence + entity tags over already-stored recalls, in place, without
# re-fetching from the sources. Run after training a new model or changing the entity gazetteer:
# `python -m scripts.reclassify`.
def main() -> None:
    session = SessionLocal()
    try:
        # All-or-nothing: every row is updated in one transaction, so a failure mid-run rolls the
        # whole batch back rather than leaving the table half-reclassified. Fine at ~26k rows.
        recalls = session.scalars(select(Recall)).all()
        for recall in recalls:
            category, confidence = classify(recall.reason_text)
            recall.category = category.value
            recall.category_confidence = confidence
            recall.entities = extract_entities(recall.reason_text)
        session.commit()
        print(f"Reclassified {len(recalls)} recalls.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
