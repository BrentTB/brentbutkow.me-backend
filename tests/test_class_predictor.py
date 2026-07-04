"""class_predictor.predict_classes — the label/confidence extraction and graceful no-model path.

The embedding model and the trained joblib are exercised only by running the scripts; the unit
under test here is how predicted probabilities become (label, confidence) pairs, and that a missing
model degrades to no prediction rather than an error. Both are pinned without touching the network
by monkeypatching the model + embedder."""

import numpy as np
import pytest

from app.modules.recalls import class_predictor


def test_predict_classes_no_model_is_graceful(monkeypatch):
    # Before a model is trained (or if the artifact is missing) every text gets no prediction.
    monkeypatch.setattr(class_predictor, "_get_model", lambda: None)
    labels, confidences = class_predictor.predict_classes(["undeclared milk", "listeria"])
    assert labels == [None, None]
    assert confidences == [0.0, 0.0]


def test_predict_classes_empty_input():
    assert class_predictor.predict_classes([]) == ([], [])


def test_predict_classes_reads_argmax_and_confidence(monkeypatch):
    class _FakeModel:
        classes_ = np.array(["Class I", "Class II", "Class III"])

        def predict_proba(self, features):
            # One row per input; the highest-probability class is the prediction.
            return np.array([[0.1, 0.7, 0.2], [0.6, 0.25, 0.15]])

    monkeypatch.setattr(class_predictor, "_get_model", lambda: _FakeModel())
    # Skip the real embedder (no model download); its output is unused by the fake.
    monkeypatch.setattr(class_predictor, "embed_texts", lambda texts: np.zeros((len(texts), 4)))

    labels, confidences = class_predictor.predict_classes(["a", "b"])
    assert labels == ["Class II", "Class I"]
    assert confidences == [pytest.approx(0.7), pytest.approx(0.6)]
