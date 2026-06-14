"""Shared row shape + field parsers for the per-source normalizers (openFDA, FSIS, FSA).

Each source's `normalize_*` maps its validated payload into a `NormalizedRecall` — the single row
shape the ingest upsert consumes — so the column set is defined once here, not in three places.
"""

from datetime import date
from typing import Any, TypedDict

from app.modules.recalls.schemas import RecallClass

# Classification values accepted verbatim from a source; anything else normalizes to None.
VALID_CLASSES = {c.value for c in RecallClass}


class NormalizedRecall(TypedDict):
    source: str
    country: str
    recall_number: str
    source_url: str | None
    event_id: str | None
    status: str | None
    classification: str | None
    product_description: str
    reason_text: str
    company_name: str | None
    state: str | None
    states: list[str] | None
    distribution_pattern: str | None
    recall_initiation_date: date | None
    report_date: date | None
    category: str
    category_confidence: float
    raw: dict[str, Any]


def parse_class(raw: str | None) -> str | None:
    return raw if raw in VALID_CLASSES else None


def parse_iso_date(raw: str | None) -> date | None:
    # Accepts an ISO date or a full ISO datetime (keeps the date part); unparseable input → None.
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None
