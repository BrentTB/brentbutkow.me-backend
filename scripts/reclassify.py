from sqlalchemy import select

from app.db import SessionLocal
from app.modules.recalls.classifier import classify
from app.modules.recalls.models import Recall


# Re-runs the classifier over already-stored recalls and updates category + confidence in place,
# without re-fetching from openFDA. Run after training a new model: `python -m scripts.reclassify`.
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
        session.commit()
        print(f"Reclassified {len(recalls)} recalls.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
