from __future__ import annotations

from datetime import date

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

    # (b) Company substring
    if sub.company:
        recall_company = (recall.company_name or "").lower()
        if sub.company.lower() not in recall_company:
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


def recall_is_new(recall: Recall, sub: Subscription) -> bool:
    """Return True iff the recall's effective date is strictly after the subscription's reference.

    Effective date: max of report_date and recall_initiation_date (whichever are non-null).
    Both null → False immediately.

    Reference point: sub.last_digest_at if non-null, else sub.confirmed_at.
    Reference None (newly activated) → True (every recall is considered new).
    """
    # Compute effective date — max of non-null date fields
    candidates = [d for d in [recall.report_date, recall.recall_initiation_date] if d is not None]
    if not candidates:
        return False
    effective_date: date = max(candidates)

    # Determine reference point
    reference = sub.last_digest_at if sub.last_digest_at is not None else sub.confirmed_at
    if reference is None:
        # Newly activated subscription — every recall is "new"
        return True

    return effective_date > reference.date()
