import re

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


# Leading listing qualifiers Health Canada puts before a brand on multi-listing recalls
# ("Various Salem Foods brand ...", "Certain Amy's brand ...") — dropped so the extracted brand is
# just the brand.
_BRAND_QUALIFIER_RE = re.compile(
    r"^(?:various|certain|some|several|a|an|the)(?:\s+|$)", re.IGNORECASE
)


def _brand(title: str | None) -> str | None:
    """Extract the brand from a CFIA title, or None when there isn't a single one.

    Health Canada titles read "{Brand} brand {Product} recalled due to {reason}", so the brand is
    the text before the " brand " marker (minus any leading listing qualifier). Titles without the
    marker are multi-brand or generic-product recalls ("Various brands of cheese ...") that have no
    single brand to lift, and yield None.
    """
    if not title or " brand " not in title:
        return None
    brand = _BRAND_QUALIFIER_RE.sub("", title.split(" brand ", 1)[0]).strip()
    return brand or None


def _product_description(record: CfiaRecord) -> str:
    """Build a product line that carries the brand.

    The feed's Product field is brand-less ("Salad + seasoning"), which reads as meaningless on its
    own in a digest, and Canadian recalls have no company_name to supply the context. So we prefix
    the brand (lifted from the Title) to the product. Multi-brand / generic recalls with no single
    brand fall back to the product name — descriptive enough on its own — then to the raw title.
    """
    product = strip_html(record.product)
    title = strip_html(record.title)
    brand = _brand(title)
    if brand and product and brand.lower() not in product.lower():
        return f"{brand} {product}"
    return product or title


def normalize_cfia(record: CfiaRecord) -> NormalizedRecall:
    # The NID is the upsert key; an empty one would collide on the composite PK. fetch_cfia already
    # filters nid-less rows, so this guards a future caller passing unfiltered records.
    if not record.nid:
        raise ValueError("CFIA record has no NID — cannot normalize (NID is the upsert key)")
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
        "recall_number": record.nid,
        "source_url": record.url,
        "event_id": None,
        # The feed has no status field; "Archived" flags retired notices, the rest are current.
        "status": "Archived" if record.archived == "1" else "Active",
        "classification": classification,
        "product_description": _product_description(record),
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
