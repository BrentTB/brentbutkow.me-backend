# Recall class predictor — model card

**Model:** Model2Vec `potion-base-8M` text embeddings + binary Logistic Regression (scikit-learn,
balanced class weights). Features are the same static neural embeddings the analytics build uses —
a learned representation, not a gazetteer.

**Task:** predict whether a recall is **Class I** (serious) or **not** (['Class I', 'not Class I']) from its
reason + product text, for recalls from countries with no native class ladder (UK, ZA, EU). The task is
binary on purpose: the three-way I/II/III split was barely learnable from text alone (II vs III
turns on facts the notice doesn't state), while Class-I-vs-rest carries real signal and is the
distinction that matters for severity.

**Training data:** 32411 recalls that carry a real class — US (FDA Class I–III) and CA (CFIA
Class 1–3, folded onto I–III at ingest), with Class II/III collapsed into the negative label. Per
country: ca: 5219, us: 27192.

**In-domain accuracy:** 0.784 — stratified 20% held-out split of the combined US+CA
corpus. How well it reproduces the Class-I-vs-rest label where it has seen that country's prose.

**Cross-country accuracy:** 0.614 raw / **0.634 balanced** — trained on US
only, tested on CA only. A genuine out-of-distribution check and the closest proxy for the UK/ZA/EU
transfer we care about (no UK/ZA/EU class labels exist). Read the *balanced* number: CA is
63% "not Class I", so a do-nothing majority guesser scores 0.632 raw
while catching zero Class I recalls — the model earns its keep by actually identifying Class I
cases (balanced accuracy over both classes), which is what the severity lift needs, at the cost of
some false positives. Expect the applied-to-UK/ZA/EU quality to be no better than this.

**How it's used:** surfaced as `predictedClass` (+ confidence) on UK/ZA/EU recalls, and — when the
prediction is Class I — it lifts that recall's severity score (scaled by confidence, bounded so it
modulates rather than anchors). Always labelled a prediction, never a regulator's ruling; UK/ZA/EU
prose is templated differently from the US/CA text the model learnt on, so treat it as a calibrated
guess.
