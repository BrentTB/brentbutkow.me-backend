import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sqlalchemy import select

from app.db import SessionLocal
from app.modules.recalls.categorize import label_category
from app.modules.recalls.classifier import MODEL_PATH
from app.modules.recalls.models import Recall
from app.modules.recalls.schemas import RecallCategory

_LABELS = [category.value for category in RecallCategory]
_CONFIDENCE_THRESHOLD = 0.6


def _build_pipeline() -> Pipeline:
    return Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    lowercase=True,
                    ngram_range=(1, 2),
                    min_df=2,
                    max_features=20000,
                    stop_words="english",
                ),
            ),
            ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ]
    )


def _load_training_data() -> tuple[list[str], list[str]]:
    session = SessionLocal()
    try:
        texts = [text for text in session.scalars(select(Recall.reason_text)).all() if text]
    finally:
        session.close()
    labels = [label_category(text).value for text in texts]
    return texts, labels


def _write_model_card(total: int, accuracy: float, reclassified: int, other_total: int) -> None:
    card = f"""# Recall category classifier — model card

**Model:** TF-IDF (1–2 grams) + multinomial Logistic Regression (scikit-learn).

**Task:** classify a recall's `reason_for_recall` text into one of {_LABELS}.

**Labels — weak supervision.** An entity-aware labeler (`label_category`) labels the corpus: the
typed entity gazetteer sets the category when a pathogen, physical hazard, or allergen is named
(pathogen wins ties, so an incidental ingredient word can't outrank the actual cause), falling back
to the v1 keyword baseline otherwise. There is **no human ground-truth set**, so the model learns to
*generalize* this taxonomy rather than beat an independent gold standard, and `category_confidence`
is the model's predicted probability for the chosen class.

**Training data:** {total} openFDA food-enforcement recalls.

**Held-out accuracy vs weak labels:** {accuracy:.3f} — how faithfully it reproduces the labeler on
a 20% test split.

**Generalization:** {reclassified} of {other_total} recalls the weak labeler left as `other` were
reclassified into a concrete category with confidence ≥ {_CONFIDENCE_THRESHOLD} — signal the labeler
missed.

**Next step (v3):** replace the weak labels with a hand-labeled sample to measure true
precision/recall and calibrate the confidence.
"""
    (MODEL_PATH.parent / "model_card.md").write_text(card)


def main() -> None:
    texts, labels = _load_training_data()
    print(f"Training on {len(texts)} recalls (weak-labeled by the entity-aware labeler).\n")

    x_train, x_test, y_train, y_test = train_test_split(
        texts, labels, test_size=0.2, random_state=42, stratify=labels
    )
    model = _build_pipeline()
    model.fit(x_train, y_train)
    predictions = model.predict(x_test)

    accuracy = accuracy_score(y_test, predictions)
    print(f"Held-out accuracy vs weak labels: {accuracy:.3f}\n")
    print(classification_report(y_test, predictions, labels=_LABELS, zero_division=0))
    print("Confusion matrix (rows = keyword label, cols = predicted):")
    print(_LABELS)
    print(confusion_matrix(y_test, predictions, labels=_LABELS))

    other_texts = [
        text
        for text, label in zip(texts, labels, strict=True)
        if label == RecallCategory.other.value
    ]
    reclassified = 0
    if other_texts:
        probabilities = model.predict_proba(other_texts)
        reclassified = sum(
            1
            for row in probabilities
            if str(model.classes_[int(row.argmax())]) != RecallCategory.other.value
            and float(row.max()) >= _CONFIDENCE_THRESHOLD
        )
    print(
        f"\nGeneralization: {reclassified} of {len(other_texts)} keyword-'other' recalls "
        f"reclassified into a real category with confidence ≥ {_CONFIDENCE_THRESHOLD}."
    )

    # Refit on the full corpus for the shipped artifact.
    final_model = _build_pipeline()
    final_model.fit(texts, labels)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, MODEL_PATH)
    _write_model_card(len(texts), accuracy, reclassified, len(other_texts))
    print(f"\nSaved model → {MODEL_PATH}")


if __name__ == "__main__":
    main()
