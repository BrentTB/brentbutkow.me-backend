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
