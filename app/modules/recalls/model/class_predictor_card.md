# Recall class predictor — model card

**Model:** Model2Vec `potion-base-8M` text embeddings + multinomial Logistic Regression
(scikit-learn, balanced class weights). Features are the same static neural embeddings the analytics
build uses — a learned representation, not a gazetteer.

**Task:** predict a recall's FDA-style class (['Class I', 'Class II', 'Class III']) from its reason + product text, for
recalls from countries with no native class system (UK, ZA).

**Training data:** 32411 recalls that carry a real class — US (FDA Class I–III) and CA (CFIA
Class 1–3, folded onto I–III at ingest). Per country: ca: 5219, us: 27192.

**In-domain accuracy:** 0.692 — stratified 20% held-out split of the combined US+CA
corpus. How well it reproduces the class where it has seen that country's prose.

**Cross-country accuracy:** 0.469 — trained on US only, tested on CA only. A genuine
out-of-distribution check and the closest proxy for the UK/ZA transfer we care about, since no
UK/ZA class labels exist. Expect the applied-to-UK/ZA accuracy to be no better than this.

**Honest limits:** the class often turns on facts not in the recall text (distribution, exposure,
firm remediation), so the ceiling is well under 1.0 — the model is strongest at Class I vs. not,
weakest at separating II from III. UK/ZA prose is templated differently from US/CA, so predictions
there are less calibrated than the in-domain number suggests. Surfaced as `predictedClass` with its
confidence and labelled a prediction, never a regulator's ruling.
