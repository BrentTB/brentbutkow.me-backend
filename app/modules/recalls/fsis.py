import html
from html.parser import HTMLParser

from curl_cffi import requests as curl_requests
from pydantic import BaseModel, ConfigDict

from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.normalize import NormalizedRecall, parse_class, parse_iso_date
from app.modules.recalls.schemas import RecallCountry, RecallSource
from app.modules.recalls.severity import score_severity

# FSIS sits behind Akamai, which 403s non-browser TLS fingerprints (plain httpx/requests, any IP).
# curl_cffi impersonates a real browser's TLS handshake, so the request gets through.
ENDPOINT = "https://www.fsis.usda.gov/fsis/api/recall/v/1"

# FSIS reports affected states by full name; the map/filter key on 2-letter codes.
_STATE_CODE = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
    "Puerto Rico": "PR",
    "Guam": "GU",
    "Virgin Islands": "VI",
}


# The external boundary — FSIS's payload, validated by Pydantic and mapped to the domain shape.
class FsisRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    field_recall_number: str = ""
    field_recall_url: str | None = None
    field_title: str = ""
    field_recall_classification: str | None = None
    field_recall_reason: list[str] = []
    field_summary: str | None = None
    field_product_items: list[str] = []
    field_establishment: list[str] = []
    field_states: list[str] = []
    field_recall_date: str | None = None
    field_active_notice: str | None = None
    langcode: str | None = None


class _TagStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def _strip_html(value: str | None) -> str:
    # FSIS values carry HTML entities and (in summaries) tags; flatten to plain text.
    if not value:
        return ""
    stripper = _TagStripper()
    stripper.feed(html.unescape(value))
    return " ".join(stripper.text.split())


def _map_states(names: list[str]) -> list[str] | None:
    codes = [_STATE_CODE[name] for name in names if name in _STATE_CODE]
    return codes or None


def normalize_fsis(record: FsisRecord) -> NormalizedRecall:
    reason_text = ", ".join(record.field_recall_reason).strip() or _strip_html(record.field_summary)
    category, confidence = classify(reason_text)
    classification = parse_class(record.field_recall_classification)
    states = _map_states(record.field_states)
    distribution_pattern = ", ".join(record.field_states) or None
    entities = extract_entities(reason_text)
    product = _strip_html(" / ".join(record.field_product_items)) or _strip_html(record.field_title)
    status = {"True": "Active", "False": "Closed"}.get(record.field_active_notice or "")
    recall_date = parse_iso_date(record.field_recall_date)
    severity_score, severity_label = score_severity(
        classification=classification,
        category=category.value,
        entities=entities,
        states=states,
        distribution_pattern=distribution_pattern,
        reason_text=reason_text,
    )
    return {
        "source": RecallSource.usda.value,
        "country": RecallCountry.us.value,
        "recall_number": record.field_recall_number,
        "source_url": record.field_recall_url,
        "event_id": None,
        "status": status,
        "classification": classification,
        "product_description": product,
        "reason_text": reason_text,
        "company_name": record.field_establishment[0] if record.field_establishment else None,
        # Single `state` only when unambiguous; the full set lives in `states` (map) + distribution.
        "state": states[0] if states and len(states) == 1 else None,
        "states": states,
        "distribution_pattern": distribution_pattern,
        "recall_initiation_date": recall_date,
        "report_date": recall_date,
        "category": category.value,
        "category_confidence": confidence,
        "severity_score": severity_score,
        "severity_label": severity_label,
        "entities": entities,
        "raw": record.model_dump(),
    }


# FSIS returns the full set in one response (no server-side paging/limit), English + Spanish mixed.
# We keep only English rows — the Spanish ones mirror them by recall number — so the ingest's
# `fetched_count` is the post-filter English count, not the raw response row total.
def fetch_fsis() -> list[FsisRecord]:
    response = curl_requests.get(ENDPOINT, impersonate="chrome", timeout=60)
    response.raise_for_status()
    data = response.json()
    return [FsisRecord.model_validate(row) for row in data if row.get("langcode") == "English"]
