"""Recall severity scoring — a transparent 0–100 composite. No model, no LLM.

The recall's own **classification** anchors the score: it is the regulator's severity judgment
(FDA Class I–III, USDA Public Health Alert, UK FSA alert type), so it sets the base *and* is the
bridge that puts US classes and UK alert types on one comparable scale. On top of that anchor sit
**content signals that read the same way in both countries**, so neither corpus collapses into one
band:

* the **cause category** nudges within the band (a Class I pathogen outranks a Class I mislabeling);
* a **lethal pathogen** (Listeria, E. coli, botulism) lifts the score on its own — enough to carry
  even a UK Product Recall into the severe band; other deadly entities (Salmonella, …) add a little;
* the **allergen risk tier** spreads allergen recalls — an undeclared peanut/tree-nut alert
  (anaphylaxis risk) scores well above an undeclared sulphite one (an intolerance);
* **reported harm** in the reason text (deaths, hospitalisations, an outbreak, reported illnesses)
  escalates — as opposed to the *potential* harm every risk statement describes;
* **geographic breadth** (nationwide / many states) adds the rest (US only — UK alerts carry no
  geography).

When a recall carries no classification, the base falls back to the cause category. Content signals
are additive and bounded so they *modulate* the regulator's call rather than override it — a Class I
can never be demoted out of the severe band. Every point is attributable to a named rule below, so
the score is fully explainable — the same design principle as the entity gazetteer and the anomaly
detector.
"""

import re

from app.modules.recalls.entities import Entity
from app.modules.recalls.schemas import EntityType, RecallCategory, RecallClass, SeverityLabel

# Base score from the classification — the regulator's own severity call. US and UK vocabularies map
# onto the same 0–100 axis here, which is what makes cross-country comparison meaningful.
_CLASS_BASE: dict[str, float] = {
    RecallClass.class_i.value: 75,  # reasonable probability of serious harm or death
    RecallClass.public_health_alert.value: 72,  # USDA — no recall yet, but a live hazard
    RecallClass.food_alert_for_action.value: 72,  # UK — consumer action required
    RecallClass.class_ii.value: 48,  # temporary or medically reversible harm
    RecallClass.product_recall.value: 45,  # UK — general product recall
    RecallClass.allergy_alert.value: 36,  # UK — undeclared allergen; the tier below spreads these
    RecallClass.class_iii.value: 22,  # unlikely to cause adverse health consequences
}

# Fallback base when a recall carries no classification: derive it from the cause category instead,
# with a wider spread than the nudge below since it is now doing the anchoring on its own.
_CATEGORY_BASE: dict[str, float] = {
    RecallCategory.pathogen.value: 62,
    RecallCategory.contaminant.value: 56,
    RecallCategory.allergen.value: 46,
    RecallCategory.foreign_material.value: 40,
    RecallCategory.mislabeling.value: 30,
    RecallCategory.other.value: 25,
}

# Small additive nudge so the cause breaks ties *within* a classification band. Kept small so it
# modulates the regulator's call rather than overriding it.
_CATEGORY_NUDGE: dict[str, float] = {
    RecallCategory.pathogen.value: 10,
    RecallCategory.contaminant.value: 8,
    RecallCategory.allergen.value: 6,
    RecallCategory.foreign_material.value: 4,
    RecallCategory.mislabeling.value: 2,
    RecallCategory.other.value: 0,
}

# Lethal pathogens — high case-fatality (Listeria, E. coli O157, botulism). A recall naming one is a
# severe hazard in its own right, whatever the regulator's paperwork calls it, so it gets a large
# bump that carries even a UK Product Recall into the severe band. This fixes the oddity where an
# undeclared allergen (US Class I) outranked a Listeria contamination (UK Product Recall) on what is
# meant to be one shared axis. Canonical values as emitted by entities.py's gazetteer.
_LETHAL_PATHOGEN: frozenset[str] = frozenset({"Listeria", "E. coli", "Clostridium botulinum"})
_LETHAL_PATHOGEN_BONUS = 20.0

# Other entities that cause severe acute illness but with lower or more variable fatality — a
# smaller bump. (Salmonella, biotoxins, the B. cereus emetic toxin, an undeclared active drug.)
_DEADLIEST: frozenset[str] = frozenset(
    {"Salmonella", "marine biotoxin", "Cereulide", "undeclared drug"}
)
_DEADLIEST_BONUS = 8.0

# Allergen risk tier. An undeclared allergen's danger depends enormously on *which* allergen: the
# big anaphylaxis/fatality drivers (peanut, tree nuts, shellfish, fish, sesame) belong far above an
# intolerance like sulphites. This is what lets a country dominated by allergy alerts (the UK) use
# the full scale instead of piling every alert into one band. Canonical values from entities.py.
_ALLERGEN = EntityType.allergen.value
_ALLERGEN_SEVERE: frozenset[str] = frozenset(
    {"peanuts", "tree nuts", "crustaceans", "molluscs", "fish", "sesame"}
)
_ALLERGEN_SEVERE_BONUS = 14.0
_ALLERGEN_LOW: frozenset[str] = frozenset({"sulphites"})  # intolerance, not anaphylaxis
_ALLERGEN_LOW_PENALTY = 12.0

# Reported harm — the text says harm actually happened (deaths, hospitalisations, an outbreak, or
# reported illnesses/reactions), not the *potential* harm ("can cause", "symptoms include") every
# risk statement carries. Country-neutral: it lets a serious recall in either corpus reach the top
# band on evidence, not just on its classification. Negation ("no illnesses reported" — the usual
# recall boilerplate) vetoes it, so a precautionary recall isn't escalated.
_HARM_OCCURRED = re.compile(
    r"\b(?:"
    r"deaths?|died|fatalit(?:y|ies)|"
    r"outbreaks?|sickened|"
    r"hospitali[sz](?:ed|ation|ations)|"
    r"(?:reports?\s+of|reported|received|confirmed|number\s+of|several|multiple|cases?\s+of|"
    r"linked\s+to|associated\s+with)\s+(?:\w+\s+){0,4}"
    r"(?:illness|illnesses|sick|reaction|reactions|infection|infections)|"
    r"(?:illnesses|reactions|infections|people|consumers)\s+(?:have|has)\s+been\s+reported|"
    r"(?:became|fallen|fell|made|taken)\s+(?:seriously\s+)?ill"
    r")\b",
    re.IGNORECASE,
)
_HARM_NEGATED = re.compile(
    r"\bno\b\s+(?:\w+\s+){0,4}"
    r"(?:illness|illnesses|reports?|reaction|reactions|infection|infections|"
    r"hospitali[sz]|deaths?|adverse|known|confirmed|reported)",
    re.IGNORECASE,
)
_HARM_BONUS = 14.0

# Geographic breadth — a nationwide recall exposes far more people than a single-state one. The
# nationwide test is word-bounded so "international" (which contains "national") can't trip it.
_NATIONWIDE = re.compile(r"\bnation(?:al|[\s-]?wide)\b", re.IGNORECASE)
_NATIONWIDE_BONUS = 10.0
_WIDE_BONUS = 6.0  # 6+ affected states
_MULTI_BONUS = 3.0  # 2–5 affected states
_WIDE_STATE_COUNT = 6

# Score → band thresholds (inclusive lower bound). Class I always clears 75 via the base alone.
_SEVERE = 75.0
_HIGH = 55.0
_MODERATE = 35.0


def _entity_bonus(entities: list[Entity]) -> float:
    # The most dangerous named hazard wins: a lethal pathogen outranks a merely serious one.
    values = {entity["value"] for entity in entities}
    if values & _LETHAL_PATHOGEN:
        return _LETHAL_PATHOGEN_BONUS
    if values & _DEADLIEST:
        return _DEADLIEST_BONUS
    return 0.0


def _allergen_adjust(entities: list[Entity]) -> float:
    # Spread allergen recalls by the riskiest named allergen: a severe one lifts the score, a recall
    # whose *only* allergen is low-risk (sulphites) drops it. Mixed → the severe one wins.
    values = {entity["value"] for entity in entities if entity["type"] == _ALLERGEN}
    if not values:
        return 0.0
    if values & _ALLERGEN_SEVERE:
        return _ALLERGEN_SEVERE_BONUS
    if values <= _ALLERGEN_LOW:
        return -_ALLERGEN_LOW_PENALTY
    return 0.0


def _harm_bonus(reason_text: str | None) -> float:
    if not reason_text or not _HARM_OCCURRED.search(reason_text):
        return 0.0
    if _HARM_NEGATED.search(reason_text):
        return 0.0
    return _HARM_BONUS


def _breadth_bonus(states: list[str] | None, distribution_pattern: str | None) -> float:
    if distribution_pattern and _NATIONWIDE.search(distribution_pattern):
        return _NATIONWIDE_BONUS
    count = len(states) if states else 0
    if count >= _WIDE_STATE_COUNT:
        return _WIDE_BONUS
    if count >= 2:
        return _MULTI_BONUS
    return 0.0


def _label(score: float) -> str:
    if score >= _SEVERE:
        return SeverityLabel.severe.value
    if score >= _HIGH:
        return SeverityLabel.high.value
    if score >= _MODERATE:
        return SeverityLabel.moderate.value
    return SeverityLabel.low.value


def score_severity(
    *,
    classification: str | None,
    category: str,
    entities: list[Entity],
    states: list[str] | None = None,
    distribution_pattern: str | None = None,
    reason_text: str | None = None,
) -> tuple[float, str]:
    """Return ``(severity_score, severity_label)`` for a recall from fields the normalizers already
    parse — so it's set at ingest time alongside the category, exactly like ``classify``.

    ``classification`` is the parsed/validated value (one of ``RecallClass``) or ``None``; an
    unrecognised value falls back to the category base, like a missing one. ``reason_text`` is the
    free text entities + category were derived from; it feeds the reported-harm signal.
    """
    class_base = _CLASS_BASE.get(classification) if classification else None
    base = (
        class_base
        if class_base is not None
        else _CATEGORY_BASE.get(category, _CATEGORY_BASE[RecallCategory.other.value])
    )

    score = base + _CATEGORY_NUDGE.get(category, 0.0)
    score += _entity_bonus(entities)
    score += _allergen_adjust(entities)
    score += _harm_bonus(reason_text)
    score += _breadth_bonus(states, distribution_pattern)

    # The regulator's top classification (Class I) can be lifted by content but never demoted out of
    # the severe band — content modulates, it doesn't override.
    if class_base is not None and class_base >= _SEVERE:
        score = max(score, _SEVERE)

    score = round(max(0.0, min(100.0, score)), 1)
    return score, _label(score)
