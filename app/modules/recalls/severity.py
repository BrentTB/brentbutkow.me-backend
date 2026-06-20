"""Recall severity scoring — a transparent 0–100 composite. No model, no LLM.

The recall's own **classification** anchors the score: it is the regulator's severity judgment
(FDA Class I–III, USDA Public Health Alert, UK FSA alert type), so it sets the base *and* is the
bridge that puts US classes and UK alert types on one comparable scale. The **cause category** then
nudges within that band (a Class I pathogen outranks a Class I mislabeling), the **deadliest named
entities** add a little more, and **geographic breadth** (nationwide / many states) adds the rest.
When a recall carries no classification, the base falls back to the cause category.

Every point is attributable to a named rule below, so the score is fully explainable — the same
design principle as the entity gazetteer and the anomaly detector.
"""

import re

from app.modules.recalls.entities import Entity
from app.modules.recalls.schemas import RecallCategory, RecallClass, SeverityLabel

# Base score from the classification — the regulator's own severity call. US and UK vocabularies map
# onto the same 0–100 axis here, which is what makes cross-country comparison meaningful.
_CLASS_BASE: dict[str, float] = {
    RecallClass.class_i.value: 75,  # reasonable probability of serious harm or death
    RecallClass.public_health_alert.value: 72,  # USDA — no recall yet, but a live hazard
    RecallClass.food_alert_for_action.value: 72,  # UK — consumer action required
    RecallClass.class_ii.value: 48,  # temporary or medically reversible harm
    RecallClass.product_recall.value: 45,  # UK — general product recall
    RecallClass.allergy_alert.value: 40,  # UK — undeclared allergen (severe only if allergic)
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

# Named entities that can cause severe acute illness or death fast — a little extra on top of the
# category. Canonical values as emitted by entities.py's gazetteer.
_DEADLIEST: frozenset[str] = frozenset(
    {
        "Listeria",
        "E. coli",
        "Clostridium botulinum",
        "Salmonella",
        "marine biotoxin",
        "undeclared drug",
    }
)
_DEADLIEST_BONUS = 6.0

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
) -> tuple[float, str]:
    """Return ``(severity_score, severity_label)`` for a recall from fields the normalizers already
    parse — so it's set at ingest time alongside the category, exactly like ``classify``.

    ``classification`` is the parsed/validated value (one of ``RecallClass``) or ``None``; an
    unrecognised value falls back to the category base, same as a missing one.
    """
    base = _CLASS_BASE.get(classification) if classification else None
    if base is None:
        base = _CATEGORY_BASE.get(category, _CATEGORY_BASE[RecallCategory.other.value])

    score = base + _CATEGORY_NUDGE.get(category, 0.0)
    if {entity["value"] for entity in entities} & _DEADLIEST:
        score += _DEADLIEST_BONUS
    score += _breadth_bonus(states, distribution_pattern)

    score = round(max(0.0, min(100.0, score)), 1)
    return score, _label(score)
