from pathlib import Path

from app.modules.recalls.categorize import label_category
from app.modules.recalls.schemas import RecallCategory

# Anchors the model/ directory. Nothing in the app loads the artifact any more (see classify below),
# but scripts/train_classifier.py still writes it here and the methodology scripts write their model
# cards alongside it via MODEL_PATH.parent.
MODEL_PATH = Path(__file__).parent / "model" / "classifier.joblib"


# Classify a recall's reason text into a category with a confidence in [0, 1].
#
# This is the entity-aware labeler, not a learned model. The TF-IDF + LogisticRegression classifier
# that used to run here was dropped in favour of the gazetteer it was trained on:
#
#   * It cost ~116 MB resident in the API process — almost none of it the model (the vectorizer and
#     coefficients are ~1.2 MB), the rest the sklearn/scipy import graph that joblib.load pulls in.
#     On a 512 MB box that was a quarter of the budget, and it contributed to the 2026-09-02
#     OOM that took the service down mid-ingest.
#   * It bought nothing measurable. Trained on weak labels produced by label_category itself, with
#     no human ground-truth set to judge either against (see model/model_card.md), it agreed with
#     the labeler on 5294 of 5296 Canadian recalls — and on both exceptions the labeler was the
#     better answer ("heavy metal (mercury)" → contaminant, not foreignMaterial).
#
# Keeping the labeler alone also drops classification from 0.24 ms to 0.02 ms per recall, which
# matters on 0.1 CPU when a single ingest normalizes thousands of rows.
#
# Confidence is 1.0 when the gazetteer/keyword baseline names a cause and 0.0 when it can't — the
# same convention the previous model-less fallback used. It is no longer a calibrated probability.
def classify(reason_text: str) -> tuple[RecallCategory, float]:
    category = label_category(reason_text)
    return category, 1.0 if category != RecallCategory.other else 0.0
