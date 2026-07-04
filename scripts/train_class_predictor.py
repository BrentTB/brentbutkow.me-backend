"""Train the cross-country recall-class predictor (app/modules/recalls/class_predictor.py).

Features are Model2Vec text embeddings; the model is a multinomial logistic regression over them.
Trains on the recalls that carry a real class — US (FDA Class I–III) + CA (CFIA Class 1–3, folded
onto I–III at ingest) — so it can be applied to the countries that don't (UK, ZA).

Two honest evals are reported and written to the model card:
  * IN-DOMAIN — a stratified held-out split of the combined US+CA corpus (how well it reproduces the
    label where it has seen that country's prose).
  * CROSS-COUNTRY — train on US only, test on CA only: a real out-of-distribution check, the closest
    proxy available for the UK/ZA transfer we actually care about (no UK/ZA ground truth exists).

Run locally with DATABASE_URL set (the embedding model downloads from Hugging Face on first use):

    python -m scripts.train_class_predictor
"""

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.db import SessionLocal
from app.modules.recalls.analytics import _compose_text
from app.modules.recalls.class_predictor import CLASS_LABELS, MODEL_PATH
from app.modules.recalls.embeddings import embed_texts
from app.modules.recalls.models import Recall

_TRAIN_COUNTRIES = ("us", "ca")


def _load() -> tuple[np.ndarray, list[str], list[str]]:
    """Composed embeddings, labels, and per-row country for the graded (US+CA) corpus."""
    session = SessionLocal()
    try:
        recalls = list(
            session.scalars(
                select(Recall)
                .options(
                    load_only(
                        Recall.country,
                        Recall.classification,
                        Recall.reason_text,
                        Recall.product_description,
                        Recall.company_name,
                    )
                )
                .where(Recall.country.in_(_TRAIN_COUNTRIES))
                .where(Recall.classification.in_(CLASS_LABELS))
                .order_by(Recall.source, Recall.recall_number)
            ).all()
        )
    finally:
        session.close()
    texts: list[str] = []
    labels: list[str] = []
    countries: list[str] = []
    for recall in recalls:
        text = _compose_text(recall.reason_text, recall.product_description, recall.company_name)
        # classification is non-null here (the query filters to CLASS_LABELS) — the guard also
        # narrows the type for mypy.
        if text and recall.classification:
            texts.append(text)
            labels.append(recall.classification)
            countries.append(recall.country)
    return embed_texts(texts), labels, countries


def _fit(features: np.ndarray, labels: list[str]) -> LogisticRegression:
    # Balanced class weights because Class III is far rarer than I/II in both corpora.
    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(features, labels)
    return model


def _write_card(total: int, in_domain: float, cross: float, by_country: dict[str, int]) -> None:
    counts = ", ".join(f"{country}: {n}" for country, n in sorted(by_country.items()))
    card = f"""# Recall class predictor — model card

**Model:** Model2Vec `potion-base-8M` text embeddings + multinomial Logistic Regression
(scikit-learn, balanced class weights). Features are the same static neural embeddings the analytics
build uses — a learned representation, not a gazetteer.

**Task:** predict a recall's FDA-style class ({CLASS_LABELS}) from its reason + product text, for
recalls from countries with no native class system (UK, ZA).

**Training data:** {total} recalls that carry a real class — US (FDA Class I–III) and CA (CFIA
Class 1–3, folded onto I–III at ingest). Per country: {counts}.

**In-domain accuracy:** {in_domain:.3f} — stratified 20% held-out split of the combined US+CA
corpus. How well it reproduces the class where it has seen that country's prose.

**Cross-country accuracy:** {cross:.3f} — trained on US only, tested on CA only. A genuine
out-of-distribution check and the closest proxy for the UK/ZA transfer we care about, since no
UK/ZA class labels exist. Expect the applied-to-UK/ZA accuracy to be no better than this.

**Honest limits:** the class often turns on facts not in the recall text (distribution, exposure,
firm remediation), so the ceiling is well under 1.0 — the model is strongest at Class I vs. not,
weakest at separating II from III. UK/ZA prose is templated differently from US/CA, so predictions
there are less calibrated than the in-domain number suggests. Surfaced as `predictedClass` with its
confidence and labelled a prediction, never a regulator's ruling.
"""
    (MODEL_PATH.parent / "class_predictor_card.md").write_text(card)


def main() -> None:
    features, labels, countries = _load()
    countries_arr = np.array(countries)
    by_country = {c: int((countries_arr == c).sum()) for c in _TRAIN_COUNTRIES}
    print(f"Training on {len(labels)} graded recalls ({by_country}).\n")

    # In-domain: stratified held-out split of the combined corpus.
    x_train, x_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.2, random_state=42, stratify=labels
    )
    in_domain_model = _fit(x_train, y_train)
    predictions = in_domain_model.predict(x_test)
    in_domain = accuracy_score(y_test, predictions)
    print(f"In-domain held-out accuracy: {in_domain:.3f}\n")
    print(classification_report(y_test, predictions, labels=CLASS_LABELS, zero_division=0))
    print("Confusion matrix (rows = true, cols = predicted):")
    print(CLASS_LABELS)
    print(confusion_matrix(y_test, predictions, labels=CLASS_LABELS))

    # Cross-country: train US, test CA — the out-of-distribution proxy for UK/ZA transfer.
    us = countries_arr == "us"
    ca = countries_arr == "ca"
    cross = float("nan")
    if us.any() and ca.any():
        cross_model = _fit(features[us], [labels[i] for i in np.where(us)[0]])
        cross_pred = cross_model.predict(features[ca])
        cross = accuracy_score([labels[i] for i in np.where(ca)[0]], cross_pred)
        print(f"\nCross-country accuracy (train US → test CA): {cross:.3f}")

    # Refit on the full graded corpus for the shipped artifact.
    final_model = _fit(features, labels)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, MODEL_PATH)
    _write_card(len(labels), in_domain, cross, by_country)
    print(f"\nSaved model → {MODEL_PATH}")


if __name__ == "__main__":
    main()
