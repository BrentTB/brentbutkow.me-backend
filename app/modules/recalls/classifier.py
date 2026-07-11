from pathlib import Path
from typing import Any

import joblib

from app.modules.recalls.categorize import label_category
from app.modules.recalls.schemas import RecallCategory

MODEL_PATH = Path(__file__).parent / "model" / "classifier.joblib"

_model: Any = None
_loaded = False


def _get_model() -> Any:
    global _model, _loaded
    if not _loaded:
        _model = joblib.load(MODEL_PATH) if MODEL_PATH.exists() else None
        _loaded = True
    return _model


# Classify a recall's reason text into a category with a confidence in [0, 1].
# Uses the trained model when present; otherwise falls back to the entity-aware labeler
# (so the app works before a model is trained, and degrades gracefully if it's missing).
def classify(reason_text: str) -> tuple[RecallCategory, float]:
    model = _get_model()
    if model is None:
        category = label_category(reason_text)
        return category, 1.0 if category != RecallCategory.other else 0.0
    probabilities = model.predict_proba([reason_text])[0]
    best = int(probabilities.argmax())
    category = RecallCategory(str(model.classes_[best]))
    confidence = float(probabilities[best])
    # Gazetteer rescue: when the model shrugs "other", defer to the high-precision entity vocabulary
    # if it names a cause. The model is trained on gazetteer-derived labels, so a term the gazetteer
    # gained *after* the last training run (e.g. EU-specific "acetamiprid", "aflatoxine") reads as
    # "other" to the model but is a confident contaminant/pathogen here — no retrain needed. Only
    # ever upgrades an "other", never overrides a confident model class.
    if category == RecallCategory.other:
        gazetteer_category = label_category(reason_text)
        if gazetteer_category != RecallCategory.other:
            return gazetteer_category, 1.0
    return category, confidence
