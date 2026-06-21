from sqlalchemy import select

from app.db import SessionLocal
from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.models import Recall
from app.modules.recalls.severity import score_severity


# Re-derives category + confidence + entity tags + severity over already-stored recalls, in place,
# without re-fetching from the sources. Run after training a new model or changing the entity
# gazetteer / severity rules: `python -m scripts.reclassify`.
# Recompute-dependency map (what to re-run when an input changes): scripts/backfill_all.py
def main() -> None:
    session = SessionLocal()
    try:
        # All-or-nothing: every row is updated in one transaction, so a failure mid-run rolls the
        # whole batch back rather than leaving the table half-reclassified. Fine at ~26k rows.
        recalls = session.scalars(select(Recall)).all()
        for recall in recalls:
            category, confidence = classify(recall.reason_text)
            entities = extract_entities(recall.reason_text)
            recall.category = category.value
            recall.category_confidence = confidence
            recall.entities = entities
            # Severity depends on classification + category + entities + geography, so re-derive it
            # here too (with the freshly computed category/entities) to keep the row consistent.
            recall.severity_score, recall.severity_label = score_severity(
                classification=recall.classification,
                category=category.value,
                entities=entities,
                states=recall.states,
                distribution_pattern=recall.distribution_pattern,
                reason_text=recall.reason_text,
            )
        session.commit()
        print(f"Reclassified {len(recalls)} recalls.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
