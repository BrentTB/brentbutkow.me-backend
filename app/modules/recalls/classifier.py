from pathlib import Path
from typing import Any

import joblib

from app.modules.recalls.categorize import categorize
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
# Uses the trained model when present; otherwise falls back to the keyword baseline
# (so the app works before a model is trained, and degrades gracefully if it's missing).
def classify(reason_text: str) -> tuple[RecallCategory, float]:
    model = _get_model()
    if model is None:
        category = categorize(reason_text)
        return category, 1.0 if category != RecallCategory.other else 0.0
    probabilities = model.predict_proba([reason_text])[0]
    best = int(probabilities.argmax())
    return RecallCategory(str(model.classes_[best])), float(probabilities[best])
