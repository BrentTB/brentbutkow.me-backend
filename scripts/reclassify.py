from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.models import Recall
from app.modules.recalls.severity import score_severity
from scripts._common import backfill_recalls


def _reclassify(recall: Recall) -> None:
    category, confidence = classify(recall.reason_text)
    entities = extract_entities(recall.reason_text)
    recall.category = category.value
    recall.category_confidence = confidence
    recall.entities = entities
    # Severity depends on classification + category + entities + geography, so re-derive it with the
    # freshly computed category/entities to keep the row consistent.
    recall.severity_score, recall.severity_label = score_severity(
        classification=recall.classification,
        category=category.value,
        entities=entities,
        states=recall.states,
        distribution_pattern=recall.distribution_pattern,
        reason_text=recall.reason_text,
    )


# Re-derives category + confidence + entity tags + severity over already-stored recalls, in place,
# without re-fetching from the sources. Run after training a new model or changing the entity
# gazetteer / severity rules: `python -m scripts.reclassify`.
# Recompute-dependency map (what to re-run when an input changes): scripts/backfill_all.py
def main() -> None:
    # batch=None → the whole corpus updates in one transaction, so a mid-run failure rolls back
    # rather than leaving the table half-reclassified.
    backfill_recalls(_reclassify, label="Reclassified", batch=None)


if __name__ == "__main__":
    main()
