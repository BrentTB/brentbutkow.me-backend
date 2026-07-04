"""Train the cross-country recall-class predictor (app/modules/recalls/class_predictor.py).

Features are Model2Vec text embeddings; the model is a binary logistic regression (Class I vs. the
rest). Trains on the recalls that carry a real class — US (FDA Class I–III) + CA (CFIA Class 1–3,
folded onto I–III at ingest, then Class II/III collapsed) — so it can be applied to the countries
that don't (UK, ZA).

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
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    classification_report,
    confusion_matrix,
)
from sklearn.model_selection import train_test_split
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.db import SessionLocal
from app.modules.recalls.analytics import _compose_text
from app.modules.recalls.class_predictor import (
    CLASS_LABELS,
    MODEL_PATH,
    NEGATIVE_CLASS,
    POSITIVE_CLASS,
)
from app.modules.recalls.embeddings import embed_texts
from app.modules.recalls.models import Recall
from app.modules.recalls.schemas import RecallClass

_TRAIN_COUNTRIES = ("us", "ca")
# The real graded classes to pull for training; each is then collapsed to the binary label.
_GRADED = [RecallClass.class_i.value, RecallClass.class_ii.value, RecallClass.class_iii.value]


def _load() -> tuple[np.ndarray, list[str], list[str]]:
    """Composed embeddings, binary labels, and per-row country for the graded (US+CA) corpus."""
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
                .where(Recall.classification.in_(_GRADED))
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
        if text and recall.classification:  # classification is non-null (query filters to _GRADED)
            texts.append(text)
            # Collapse Class II/III into the negative label — the binary Class-I-vs-rest task.
            labels.append(
                POSITIVE_CLASS if recall.classification == POSITIVE_CLASS else NEGATIVE_CLASS
            )
            countries.append(recall.country)
    return embed_texts(texts), labels, countries


def _fit(features: np.ndarray, labels: list[str]) -> LogisticRegression:
    # Balanced class weights — Class I is the minority against the collapsed II+III negative.
    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(features, labels)
    return model


def _write_card(
    total: int,
    in_domain: float,
    cross: float,
    cross_balanced: float,
    ca_majority: float,
    by_country: dict[str, int],
) -> None:
    counts = ", ".join(f"{country}: {n}" for country, n in sorted(by_country.items()))
    card = f"""# Recall class predictor — model card

**Model:** Model2Vec `potion-base-8M` text embeddings + binary Logistic Regression (scikit-learn,
balanced class weights). Features are the same static neural embeddings the analytics build uses —
a learned representation, not a gazetteer.

**Task:** predict whether a recall is **Class I** (serious) or **not** ({CLASS_LABELS}) from its
reason + product text, for recalls from countries with no native class ladder (UK, ZA). The task is
binary on purpose: the three-way I/II/III split was barely learnable from text alone (II vs III
turns on facts the notice doesn't state), while Class-I-vs-rest carries real signal and is the
distinction that matters for severity.

**Training data:** {total} recalls that carry a real class — US (FDA Class I–III) and CA (CFIA
Class 1–3, folded onto I–III at ingest), with Class II/III collapsed into the negative label. Per
country: {counts}.

**In-domain accuracy:** {in_domain:.3f} — stratified 20% held-out split of the combined US+CA
corpus. How well it reproduces the Class-I-vs-rest label where it has seen that country's prose.

**Cross-country accuracy:** {cross:.3f} raw / **{cross_balanced:.3f} balanced** — trained on US
only, tested on CA only. A genuine out-of-distribution check and the closest proxy for the UK/ZA
transfer we care about (no UK/ZA class labels exist). Read the *balanced* number: CA is
{ca_majority:.0%} "not Class I", so a do-nothing majority guesser scores {ca_majority:.3f} raw
while catching zero Class I recalls — the model earns its keep by actually identifying Class I
cases (balanced accuracy over both classes), which is what the severity lift needs, at the cost of
some false positives. Expect the applied-to-UK/ZA quality to be no better than this.

**How it's used:** surfaced as `predictedClass` (+ confidence) on UK/ZA recalls, and — when the
prediction is Class I — it lifts that recall's severity score (scaled by confidence, bounded so it
modulates rather than anchors). Always labelled a prediction, never a regulator's ruling; UK/ZA
prose is templated differently from the US/CA text the model learnt on, so treat it as a calibrated
guess.
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
    cross = cross_balanced = ca_majority = float("nan")
    if us.any() and ca.any():
        ca_labels = [labels[i] for i in np.where(ca)[0]]
        cross_model = _fit(features[us], [labels[i] for i in np.where(us)[0]])
        cross_pred = cross_model.predict(features[ca])
        cross = accuracy_score(ca_labels, cross_pred)
        cross_balanced = balanced_accuracy_score(ca_labels, cross_pred)
        ca_majority = ca_labels.count(NEGATIVE_CLASS) / len(ca_labels)
        print(
            f"\nCross-country (train US → test CA): {cross:.3f} raw / {cross_balanced:.3f} balanced"
            f" (CA majority-guess baseline: {ca_majority:.3f} raw)"
        )

    # Refit on the full graded corpus for the shipped artifact.
    final_model = _fit(features, labels)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, MODEL_PATH)
    _write_card(len(labels), in_domain, cross, cross_balanced, ca_majority, by_country)
    print(f"\nSaved model → {MODEL_PATH}")


if __name__ == "__main__":
    main()
