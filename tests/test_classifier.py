import numpy as np

from app.modules.recalls import classifier
from app.modules.recalls.classifier import classify
from app.modules.recalls.schemas import RecallCategory


def test_falls_back_to_keyword_baseline_without_a_model(monkeypatch):
    monkeypatch.setattr(classifier, "_get_model", lambda: None)
    assert classify("Product contains undeclared milk.") == (RecallCategory.allergen, 1.0)
    assert classify("Quality defect of unknown origin.") == (RecallCategory.other, 0.0)


def test_classify_returns_a_valid_category_and_confidence():
    # Works whether the trained artifact is present (model) or not (keyword fallback).
    category, confidence = classify("Potential Listeria monocytogenes contamination.")
    assert isinstance(category, RecallCategory)
    assert 0.0 <= confidence <= 1.0


class _OtherModel:
    """A stand-in model that always predicts 'other' — the case the gazetteer rescue targets."""

    classes_ = np.array(["other", "contaminant"])

    def predict_proba(self, _texts):
        return np.array([[0.9, 0.1]])


def test_gazetteer_rescues_an_other_prediction_when_it_names_a_cause(monkeypatch):
    # The model was trained before the gazetteer gained EU terms, so it reads "acetamiprid" as
    # "other"; the high-precision gazetteer names it a contaminant and must win.
    monkeypatch.setattr(classifier, "_get_model", lambda: _OtherModel())
    assert classify("Acetamiprid in pears from Turkey") == (RecallCategory.contaminant, 1.0)


def test_gazetteer_rescue_leaves_a_genuine_other_alone(monkeypatch):
    # No entity to rescue with → the model's "other" stands.
    monkeypatch.setattr(classifier, "_get_model", lambda: _OtherModel())
    category, _ = classify("Unauthorised novel food ingredient, no named hazard.")
    assert category == RecallCategory.other


def test_gazetteer_rescue_never_overrides_a_confident_model_class(monkeypatch):
    class _AllergenModel:
        classes_ = np.array(["other", "allergen"])

        def predict_proba(self, _texts):
            return np.array([[0.2, 0.8]])  # confident allergen

    monkeypatch.setattr(classifier, "_get_model", lambda: _AllergenModel())
    # Even though the reason names a pathogen, a confident non-"other" model class is kept as-is.
    category, confidence = classify("Salmonella found")
    assert category == RecallCategory.allergen
    assert confidence == 0.8
