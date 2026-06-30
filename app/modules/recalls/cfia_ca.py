import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.normalize import NormalizedRecall, parse_iso_date, strip_html
from app.modules.recalls.schemas import RecallClass, RecallCountry, RecallSource
from app.modules.recalls.severity import score_severity

# Health Canada's "Recalls and Safety Alerts" open-data export — one daily-refreshed JSON file
# covering every category (food, consumer products, vehicles, drugs, medical devices, …). We keep
# only the food slice; see FOOD_ORG below.
ENDPOINT = (
    "https://recalls-rappels.canada.ca/sites/default/files/"
    "opendata-donneesouvertes/HCRSAMOpenData.json"
)

# The food filter. Every record names the issuing organization; "CFIA" (Canadian Food Inspection
# Agency) is the food-recall authority, so its rows are exactly the human-food recalls — including
# the ones tagged with a food *sub*category (Dairy, Fresh, Candy, …) rather than the bare "Food",
# which a Category=="Food" filter would miss. ~5k rows of the ~34k-row file.
FOOD_ORG = "CFIA"


# The external boundary — Health Canada's record, validated by Pydantic and mapped to the domain
# shape. Field names carry spaces/capitals in the feed, so each maps via an alias. Every text field
# is nullable: the feed sends an explicit `null` for missing values (not just an absent key), which
# the normalizer and strip_html() already tolerate.
class CfiaRecord(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    nid: str | None = Field(default=None, alias="NID")
    title: str | None = Field(default=None, alias="Title")
    url: str | None = Field(default=None, alias="URL")
    organization: str | None = Field(default=None, alias="Organization")
    product: str | None = Field(default=None, alias="Product")
    issue: str | None = Field(default=None, alias="Issue")
    recall_class: str | None = Field(default=None, alias="Recall class")
    last_updated: str | None = Field(default=None, alias="Last updated")
    archived: str | None = Field(default=None, alias="Archived")


# CFIA grades food recalls Class 1-3 on the same risk ladder as the FDA's Class I-III (1/I = highest
# health risk), so we fold them onto the existing classification values — the severity model and the
# classification facet then treat CA, US and UK on one scale. A combined value ("Class 1 - Class 2")
# takes the more severe end; "", "--" and anything unrecognized become None.
def _classification(raw: str | None) -> str | None:
    if not raw:
        return None
    if "Class 1" in raw:
        return RecallClass.class_i.value
    if "Class 2" in raw:
        return RecallClass.class_ii.value
    if "Class 3" in raw:
        return RecallClass.class_iii.value
    return None


def normalize_cfia(record: CfiaRecord) -> NormalizedRecall:
    reason_text = strip_html(record.issue) or strip_html(record.title)
    category, confidence = classify(reason_text)
    classification = _classification(record.recall_class)
    entities = extract_entities(reason_text)
    # The feed exposes a single "Last updated" date, so it serves as both the report and initiation
    # date — unlike FDA, there's no separate initiation date to draw on.
    updated = parse_iso_date(record.last_updated)
    # Canadian recalls carry no firm name or geography in the feed, so severity rests on the class,
    # cause, entities, allergen tier, and any reported harm — the same inputs the UK path uses.
    severity_score, severity_label = score_severity(
        classification=classification,
        category=category.value,
        entities=entities,
        reason_text=reason_text,
    )
    return {
        "source": RecallSource.cfia.value,
        "country": RecallCountry.ca.value,
        "recall_number": record.nid or "",
        "source_url": record.url,
        "event_id": None,
        # The feed has no status field; "Archived" flags retired notices, the rest are current.
        "status": "Archived" if record.archived == "1" else "Active",
        "classification": classification,
        "product_description": strip_html(record.product) or strip_html(record.title),
        "reason_text": reason_text,
        # Not in the feed — Canada doesn't appear in the company leaderboard / type-ahead.
        "company_name": None,
        # No Canadian geography in the feed — like UK, these don't appear on the US state map.
        "state": None,
        "states": None,
        "distribution_pattern": None,
        "recall_initiation_date": updated,
        "report_date": updated,
        "category": category.value,
        "category_confidence": confidence,
        "severity_score": severity_score,
        "severity_label": severity_label,
        "entities": entities,
        "raw": record.model_dump(),
    }


# Downloads the full export (~15 MB) and keeps the CFIA food rows. The whole corpus arrives in one
# file, so each run is a full re-sync — the upsert is idempotent, and there's no pagination or
# separate backfill to manage.
def fetch_cfia() -> list[CfiaRecord]:
    response = httpx.get(ENDPOINT, timeout=120, follow_redirects=True)
    response.raise_for_status()
    records = (CfiaRecord.model_validate(item) for item in response.json())
    # Keep CFIA food rows that carry a NID — the NID is the upsert key, so a row without one is
    # unusable (and shouldn't occur in practice).
    return [record for record in records if record.organization == FOOD_ORG and record.nid]
