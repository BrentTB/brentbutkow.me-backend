"""Shared row shape + field parsers for the per-source normalizers (openFDA, FSIS, FSA).

Each source's `normalize_*` maps its validated payload into a `NormalizedRecall` — the single row
shape the ingest upsert consumes — so the column set is defined once here, not in three places.
"""

import html
from datetime import date
from html.parser import HTMLParser
from typing import Any, TypedDict

from app.modules.recalls.entities import Entity
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
    severity_score: float
    severity_label: str
    entities: list[Entity]
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


class _TagStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def strip_html(value: str | None) -> str:
    # Source payloads carry HTML entities and tags (FSIS summaries, NCC article bodies); flatten to
    # plain text — unescape entities, drop tags, and collapse whitespace.
    if not value:
        return ""
    stripper = _TagStripper()
    stripper.feed(html.unescape(value))
    return " ".join(stripper.text.split())
