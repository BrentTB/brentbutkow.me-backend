"""Cross-country recall-class prediction — a Class I/II/III guess for countries with no native
class system (UK, ZA), from a model trained on the countries that do (US, CA). No LLM.

The FDA (Class I–III) and CFIA (Class 1–3, folded onto I–III at ingest) both grade recalls on one
health-risk ladder; the UK FSA and South Africa's NCC do not. This trains a classifier on the
US + CA recalls that carry a real class and applies it to UK/ZA recalls, so severity can be reasoned
about on one axis across all four countries. Features are the same Model2Vec text embeddings the
analytics build uses (app/modules/recalls/embeddings.py) — a static neural representation, not a
gazetteer — so this is genuinely learned, not keyword rules.

Offline only, like the analytics build: `predict_classes` loads the committed model
(model/class_predictor.joblib, trained by scripts/train_class_predictor.py) and
`rebuild_predictions` materialises the guess into `recalls.predicted_class` /
`predicted_class_confidence` (scripts/build_predictions.py). Serving is a plain column read — the
model is never loaded on the request path. A missing model degrades to no prediction.

Honest limits (see the model card): the class often turns on facts absent from the recall text, and
UK/ZA prose differs from the US/CA text the model learnt on (domain shift), so treat the output as a
calibrated guess with its confidence, never a regulator's ruling.
"""

from pathlib import Path
from typing import Any, cast

import joblib
from sqlalchemy import Table, bindparam, select, update
from sqlalchemy.orm import Session, load_only

from app.modules.recalls.analytics import _compose_text
from app.modules.recalls.embeddings import embed_texts
from app.modules.recalls.models import Recall
from app.modules.recalls.schemas import RecallClass

MODEL_PATH = Path(__file__).parent / "model" / "class_predictor.joblib"

# Only the graded classes are predicted (US Public Health Alerts and unclassified rows are excluded
# from training) — the label space is exactly these three.
CLASS_LABELS = [RecallClass.class_i.value, RecallClass.class_ii.value, RecallClass.class_iii.value]

# Countries with no native class system, so a prediction is meaningful there. US/CA carry a real
# `classification`, so they are never overwritten with a guess.
PREDICT_COUNTRIES = ("uk", "za")

_DB_CHUNK = 1000

_model: Any = None
_loaded = False


def _get_model() -> Any:
    global _model, _loaded
    if not _loaded:
        _model = joblib.load(MODEL_PATH) if MODEL_PATH.exists() else None
        _loaded = True
    return _model


def predict_classes(texts: list[str]) -> tuple[list[str | None], list[float]]:
    """Predict a class label + confidence in [0, 1] for each composed recall text.

    Embeds with the same model the analytics build uses, then reads the trained logistic-regression
    probabilities. Returns (None, 0.0) for every text when no model is committed, so the pipeline
    degrades gracefully (like the category classifier)."""
    model = _get_model()
    if model is None or not texts:
        return [None] * len(texts), [0.0] * len(texts)
    probabilities = model.predict_proba(embed_texts(texts))
    labels: list[str | None] = []
    confidences: list[float] = []
    for row in probabilities:
        best = int(row.argmax())
        labels.append(str(model.classes_[best]))
        confidences.append(round(float(row[best]), 4))
    return labels, confidences


def rebuild_predictions(session: Session) -> dict[str, int]:
    """Predict + materialise `predicted_class` / `predicted_class_confidence` for the countries with
    no native class system (UK, ZA). Called by scripts/build_predictions.py. One transaction.

    A recall with no usable text (and every recall when no model is committed) gets a NULL
    prediction rather than a low-signal guess. Like the analytics build, the write preserves
    `updated_at` — predicted_class is a derived column, not a source change."""
    recalls = list(
        session.scalars(
            select(Recall)
            .options(
                load_only(
                    Recall.source,
                    Recall.recall_number,
                    Recall.reason_text,
                    Recall.product_description,
                    Recall.company_name,
                )
            )
            .where(Recall.country.in_(PREDICT_COUNTRIES))
            .order_by(Recall.source, Recall.recall_number)
        ).all()
    )
    texts = [_compose_text(r.reason_text, r.product_description, r.company_name) for r in recalls]
    # Predict only the recalls with usable text; the rest stay NULL (an empty embedding would be a
    # meaningless guess at the softmax origin).
    usable = [i for i, text in enumerate(texts) if text and text.strip()]
    labels, confidences = predict_classes([texts[i] for i in usable])
    predicted: list[str | None] = [None] * len(recalls)
    confidence: list[float | None] = [None] * len(recalls)
    for position, original in enumerate(usable):
        predicted[original] = labels[position]
        confidence[original] = confidences[position]

    rows = [
        {
            "b_source": recall.source,
            "b_number": recall.recall_number,
            "b_class": predicted[i],
            "b_conf": confidence[i],
        }
        for i, recall in enumerate(recalls)
    ]

    # Core UPDATE preserving updated_at (see rebuild_analytics for why): derived columns must not
    # look like a source change to the stats/analytics staleness checks.
    recall_table = cast(Table, Recall.__table__)
    for start in range(0, len(rows), _DB_CHUNK):
        session.execute(
            update(recall_table)
            .where(recall_table.c.source == bindparam("b_source"))
            .where(recall_table.c.recall_number == bindparam("b_number"))
            .values(
                predicted_class=bindparam("b_class"),
                predicted_class_confidence=bindparam("b_conf"),
                updated_at=recall_table.c.updated_at,
            ),
            rows[start : start + _DB_CHUNK],
        )
    session.commit()
    session.expire_all()
    return {"recalls": len(recalls), "predicted": len(usable)}
