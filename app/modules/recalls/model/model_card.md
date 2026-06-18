# Recall category classifier — model card

**Model:** TF-IDF (1–2 grams) + multinomial Logistic Regression (scikit-learn).

**Task:** classify a recall's `reason_for_recall` text into one of ['allergen', 'pathogen', 'foreignMaterial', 'mislabeling', 'contaminant', 'other'].

**Labels — weak supervision.** An entity-aware labeler (`label_category`) labels the corpus: the
typed entity gazetteer sets the category when a pathogen, physical hazard, or allergen is named
(pathogen wins ties, so an incidental ingredient word can't outrank the actual cause), falling back
to the v1 keyword baseline otherwise. There is **no human ground-truth set**, so the model learns to
*generalize* this taxonomy rather than beat an independent gold standard, and `category_confidence`
is the model's predicted probability for the chosen class.

**Training data:** 28542 openFDA food-enforcement recalls.

**Held-out accuracy vs weak labels:** 0.988 — how faithfully it reproduces the labeler on
a 20% test split.

**Generalization:** 18 of 5139 recalls the weak labeler left as `other` were
reclassified into a concrete category with confidence ≥ 0.6 — signal the labeler
missed.

**Next step (v3):** replace the weak labels with a hand-labeled sample to measure true
precision/recall and calibrate the confidence.
