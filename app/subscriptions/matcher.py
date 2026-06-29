from __future__ import annotations

from app.modules.recalls.models import Recall
from app.subscriptions.models import SEVERITY_ORDER, Subscription


def recall_matches(recall: Recall, sub: Subscription) -> bool:
    """Return True iff all non-empty filter criteria on the subscription are satisfied.

    Criteria are applied as a conjunction — a single mismatch returns False immediately.
    Any criterion whose subscription field is empty/null is skipped (no constraint).
    All string comparisons are case-insensitive.
    """
    # (a) Entity filter
    if sub.entities:
        sub_entities_lower = {e.lower() for e in sub.entities}
        recall_entity_values = {entity["value"].lower() for entity in (recall.entities or [])}
        if not recall_entity_values.intersection(sub_entities_lower):
            return False

    # (b) Company substring — match if the recall's company contains any of the subscribed names
    if sub.companies:
        recall_company = (recall.company_name or "").lower()
        if not any(c.lower() in recall_company for c in sub.companies):
            return False

    # (c) Country membership
    if sub.countries:
        if recall.country not in sub.countries:
            return False

    # (d) Category membership
    if sub.categories:
        if recall.category not in sub.categories:
            return False

    # (e) Minimum severity threshold
    if sub.min_severity:
        try:
            recall_idx = SEVERITY_ORDER.index(recall.severity_label)
            sub_idx = SEVERITY_ORDER.index(sub.min_severity)
        except ValueError:
            return False
        if recall_idx < sub_idx:
            return False

    return True
