from curl_cffi import requests as curl_requests
from pydantic import BaseModel, ConfigDict

from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.normalize import NormalizedRecall, parse_iso_date
from app.modules.recalls.schemas import RecallClass, RecallCountry, RecallSource

ENDPOINT = "https://data.food.gov.uk/food-alerts/id"
_PAGE = 200  # FSA paginates via _limit/_offset
# Safety ceiling on pagination. The live dataset (alerts since 2018) is a few thousand rows, so this
# is huge headroom — it exists only so an endpoint that ignores `_offset` can't loop/OOM forever.
_MAX_OFFSET = 20_000

# FSA alert type (last path segment of the non-Alert `type` URI) → our classification value.
_TYPE_CLASS = {
    "PRIN": RecallClass.product_recall.value,
    "AA": RecallClass.allergy_alert.value,
    "FAFA": RecallClass.food_alert_for_action.value,
}


# The external boundary — FSA's payload, validated by Pydantic and mapped to the domain shape.
class FsaProblem(BaseModel):
    model_config = ConfigDict(extra="allow")
    riskStatement: str | None = None


class FsaProduct(BaseModel):
    model_config = ConfigDict(extra="allow")
    productName: str | None = None


class FsaBusiness(BaseModel):
    model_config = ConfigDict(extra="allow")
    commonName: str | None = None


class FsaStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    label: str | None = None


class FsaRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    notation: str = ""
    title: str = ""
    created: str | None = None
    type: list[str] = []
    status: FsaStatus | None = None
    alertURL: str | None = None
    reportingBusiness: FsaBusiness | None = None
    problem: list[FsaProblem] = []
    productDetails: list[FsaProduct] = []


def _classification(type_uris: list[str]) -> str | None:
    for uri in type_uris:
        code = uri.rsplit("/", 1)[-1]
        if code in _TYPE_CLASS:
            return _TYPE_CLASS[code]
    return None


def normalize_fsa(record: FsaRecord) -> NormalizedRecall:
    reason_text = " ".join(p.riskStatement for p in record.problem if p.riskStatement).strip()
    if not reason_text:
        reason_text = record.title
    category, confidence = classify(reason_text)
    product = " / ".join(p.productName for p in record.productDetails if p.productName)
    created = parse_iso_date(record.created)
    return {
        "source": RecallSource.uk.value,
        "country": RecallCountry.uk.value,
        "recall_number": record.notation,
        "source_url": record.alertURL,
        "event_id": None,
        "status": record.status.label if record.status else None,
        "classification": _classification(record.type),
        "product_description": product or record.title,
        "reason_text": reason_text,
        "company_name": record.reportingBusiness.commonName if record.reportingBusiness else None,
        # UK alerts carry no US state — they don't appear on the US map.
        "state": None,
        "states": None,
        "distribution_pattern": None,
        "recall_initiation_date": created,
        "report_date": created,
        "category": category.value,
        "category_confidence": confidence,
        "entities": extract_entities(reason_text),
        "raw": record.model_dump(),
    }


# Paginates _limit/_offset until a short page. Modest dataset (FSA alerts since 2018).
def fetch_fsa() -> list[FsaRecord]:
    records: list[FsaRecord] = []
    offset = 0
    while offset <= _MAX_OFFSET:
        response = curl_requests.get(
            ENDPOINT,
            params={"_limit": _PAGE, "_offset": offset},
            headers={"Accept": "application/json"},
            impersonate="chrome",
            timeout=60,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        if not items:
            break
        records.extend(FsaRecord.model_validate(item) for item in items)
        if len(items) < _PAGE:
            break
        offset += _PAGE
    return records
