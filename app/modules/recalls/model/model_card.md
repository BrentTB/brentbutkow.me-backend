# Recall category classifier — model card

**Model:** TF-IDF (1–2 grams) + multinomial Logistic Regression (scikit-learn).

**Task:** classify a recall's `reason_for_recall` text into one of ['allergen', 'pathogen', 'foreignMaterial', 'mislabeling', 'other'].

**Labels — weak supervision.** The v1 keyword baseline (`categorize`) labels the corpus; there is
**no human ground-truth set**. So the model learns to *generalize* the keyword taxonomy rather than
beat an independent gold standard, and `category_confidence` is the model's predicted probability
for the chosen class.

**Training data:** 26000 openFDA food-enforcement recalls.

**Held-out accuracy vs keyword labels:** 0.988 — how faithfully it reproduces the rules on
a 20% test split.

**Generalization:** 11 of 5145 recalls the keyword rules left as `other` were
reclassified into a concrete category with confidence ≥ 0.6 — signal the keyword
rules missed.

**Next step (v3):** replace the weak labels with a hand-labeled sample to measure true
precision/recall and calibrate the confidence.
