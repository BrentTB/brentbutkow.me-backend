from datetime import date

import httpx
from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.modules.recalls.classifier import classify
from app.modules.recalls.schemas import RecallClass, RecallCountry, RecallSource

ENDPOINT = "https://api.fda.gov/food/enforcement.json"
_VALID_CLASSES = {c.value for c in RecallClass}


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


def _parse_class(raw: str | None) -> str | None:
    return raw if raw in _VALID_CLASSES else None


def normalize_recall(record: OpenFdaRecord) -> dict:
    reason_text = record.reason_for_recall or ""
    category, confidence = classify(reason_text)
    return {
        "source": RecallSource.fda.value,
        "country": RecallCountry.us.value,
        "recall_number": record.recall_number,
        "source_url": None,
        "event_id": record.event_id,
        "status": record.status,
        "classification": _parse_class(record.classification),
        "product_description": record.product_description or "",
        "reason_text": reason_text,
        "company_name": record.recalling_firm,
        "state": record.state,
        "states": [record.state] if record.state else None,
        "distribution_pattern": record.distribution_pattern,
        "recall_initiation_date": _parse_date(record.recall_initiation_date),
        "report_date": _parse_date(record.report_date),
        "category": category.value,
        "category_confidence": confidence,
        "raw": record.model_dump(),
    }


# openFDA caps a single request at limit=1000 and skip at 25000 (~26k records reachable this way).
MAX_LIMIT_PER_REQUEST = 1000
MAX_SKIP = 25000


def _fetch_page(skip: int, limit: int) -> list[OpenFdaRecord]:
    params = {"sort": "report_date:desc", "skip": str(skip), "limit": str(limit)}
    if settings.openfda_api_key:
        params["api_key"] = settings.openfda_api_key
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
