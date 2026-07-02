from datetime import date

import httpx
from pydantic import BaseModel, ConfigDict

from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.normalize import (
    NormalizedRecall,
    parse_class,
    parse_us_state,
    strip_html,
)
from app.modules.recalls.schemas import RecallCountry, RecallSource
from app.modules.recalls.severity import score_severity

ENDPOINT = "https://api.fda.gov/food/enforcement.json"


# The external boundary — openFDA's payload, validated by Pydantic and mapped to the domain shape.
class OpenFdaRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    recall_number: str
    event_id: str | None = None
    status: str | None = None
    classification: str | None = None
    product_description: str | None = None
    reason_for_recall: str | None = None
    recalling_firm: str | None = None
    state: str | None = None
    distribution_pattern: str | None = None
    recall_initiation_date: str | None = None
    report_date: str | None = None


class OpenFdaResponse(BaseModel):
    results: list[OpenFdaRecord] = []


def _parse_date(raw: str | None) -> date | None:
    if not raw or len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def normalize_recall(record: OpenFdaRecord) -> NormalizedRecall:
    # openFDA payloads carry HTML entities ("Reser&#039;s Fine Foods"); decode once here so every
    # consumer — API, dashboard, alert emails — and the downstream classifier/entity extraction all
    # see plain text. Matches what the CFIA/FSIS normalizers already do via strip_html.
    reason_text = strip_html(record.reason_for_recall)
    category, confidence = classify(reason_text)
    classification = parse_class(record.classification)
    # The recalling firm's state — only a real US code feeds the map / filter. Foreign firms report
    # a province ("Ontario") or "N/A", which would otherwise pollute the US state facet.
    state = parse_us_state(record.state)
    states = [state] if state else None
    entities = extract_entities(reason_text)
    severity_score, severity_label = score_severity(
        classification=classification,
        category=category.value,
        entities=entities,
        states=states,
        distribution_pattern=record.distribution_pattern,
        reason_text=reason_text,
    )
    return {
        "source": RecallSource.fda.value,
        "country": RecallCountry.us.value,
        "recall_number": record.recall_number,
        "source_url": None,
        "event_id": record.event_id,
        "status": record.status,
        "classification": classification,
        "product_description": strip_html(record.product_description),
        "reason_text": reason_text,
        # strip_html returns "" for a missing firm; keep the column nullable as before.
        "company_name": strip_html(record.recalling_firm) or None,
        "state": state,
        "states": states,
        "distribution_pattern": record.distribution_pattern,
        "recall_initiation_date": _parse_date(record.recall_initiation_date),
        "report_date": _parse_date(record.report_date),
        "category": category.value,
        "category_confidence": confidence,
        "severity_score": severity_score,
        "severity_label": severity_label,
        "entities": entities,
        "raw": record.model_dump(),
    }


# openFDA caps a single request at limit=1000 and skip at 25000 (~26k records reachable this way).
MAX_LIMIT_PER_REQUEST = 1000
MAX_SKIP = 25000


def _fetch_page(skip: int, limit: int) -> list[OpenFdaRecord]:
    params = {"sort": "report_date:desc", "skip": str(skip), "limit": str(limit)}
    response = httpx.get(ENDPOINT, params=params, timeout=30)
    # openFDA returns 404 when a query has no (more) results — treat as end-of-data, not an error.
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return OpenFdaResponse.model_validate(response.json()).results


# Pulls the most recent `limit` recalls, newest first, paginating across requests as needed.
# A daily ingest uses a small limit; the one-time backfill passes a large one to seed history.
def fetch_enforcement(limit: int = 1000) -> list[OpenFdaRecord]:
    records: list[OpenFdaRecord] = []
    skip = 0
    while len(records) < limit and skip <= MAX_SKIP:
        page = min(MAX_LIMIT_PER_REQUEST, limit - len(records))
        batch = _fetch_page(skip, page)
        if not batch:
            break
        records.extend(batch)
        skip += len(batch)
        if len(batch) < page:  # last page reached
            break
    return records[:limit]
