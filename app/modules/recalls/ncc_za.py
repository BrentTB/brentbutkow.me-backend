"""South Africa recall source — the National Consumer Commission (NCC).

South Africa has no recall API. The NCC publishes recall notices on its WordPress site, so the
practical "API" is the WordPress REST API (`/wp-json/wp/v2/posts`): each post is JSON carrying the
article HTML (`content.rendered`), an ISO `date`, and a stable `slug` we use as the identifier.
That's structured and theme-independent — far less brittle than scraping rendered pages.

Two NCC-specific problems shaped this module:

* **Mixed product types.** The NCC recalls cars, tyres, electronics, cosmetics, medicine and food
  under the same `product-recall-*` / `product-safety-recall-*` slugs, and its categories/tags don't
  separate them (recalls sit under "Media Statements" with no tags). Recall Radar is a *food*
  platform, so `is_food_recall` keeps human food only — a deny-list of the dominant non-food classes
  (vehicles, electronics, pet food, cosmetics) plus a positive food / food-safety allow-list over
  the title + body. Validated against the full NCC history: keeps the human-food recalls, drops the
  vehicles, electronics, cosmetics and pet food.
* **Boilerplate stubs.** Some older recalls have a `product-recall-*` post whose body is just a
  "contact us" placeholder (the real write-up is a separate, non-prefixed post). `_reason_text`
  detects that boilerplate and falls back to the title, so the recall still ingests with a sensible
  description rather than the placeholder.

NCC issues no FDA-style classification, no geography, and no recall number, so those map to `None`
and the slug becomes the `recall_number`. The full post (including `content.rendered`) is kept in
`raw`, so extra fields (batch numbers, provinces) can be re-extracted later without re-crawling.
"""

import re

from curl_cffi import requests as curl_requests
from pydantic import BaseModel, ConfigDict, Field

from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.normalize import NormalizedRecall, parse_iso_date, strip_html
from app.modules.recalls.schemas import RecallCountry, RecallSource
from app.modules.recalls.severity import score_severity

ENDPOINT = "https://thencc.org.za/wp-json/wp/v2/posts"
_PAGE = 100  # WordPress caps per_page at 100
# Only the fields we use — keeps the payload small while still carrying the article HTML for `raw`.
_FIELDS = "id,slug,link,date,modified,title,content,excerpt,categories,tags"
# Safety ceiling on pagination — the live site is ~4 pages, so this is huge headroom; it only stops
# a runaway loop if the X-WP-TotalPages header is ever missing or wrong.
_MAX_PAGES = 20

# A recall post — the NCC uses these two slug prefixes for every recall notice.
_RECALL_SLUG_PREFIXES = ("product-recall-", "product-safety-recall-")

# Non-food classes the NCC recalls under the same slugs. A match in the title/body drops the post
# before the food allow-list runs (so "pet food" can't slip through on the word "food").
_NON_FOOD_DENY = (
    "pet food",
    "dog food",
    "cat food",
    "dog and cat",
    "animal feed",
    "dry dog",
    "dry cat",
    "vehicle",
    "truck",
    "tyre",
    " tire",
    "helmet",
    "kettle",
    "electrode",
    "medical device",
    "power bank",
    "charging",
    "spray paint",
    "galvanising",
    "shampoo",
    "relaxer",
)

# Positive human-food signals: food/drink nouns plus the food-safety vocabulary that only appears in
# food recalls (a named pathogen/contaminant/allergen). A post must match one of these to be kept.
_FOOD_ALLOW = (
    "peanut",
    "infant formula",
    "formula",
    "soup",
    "beans",
    "stir fry",
    "puffs",
    "squash",
    "snack",
    "beverage",
    "juice",
    "cereal",
    "chocolate",
    "confection",
    "spice",
    "foodstuff",
    "edible",
    "for human consumption",
    "best before",
    "aflatoxin",
    "listeria",
    "salmonella",
    "cereulide",
    "botulism",
    "norovirus",
    "hepatitis a",
    "undeclared allergen",
    "food safety",
)

# Placeholder body some recalls carry instead of a write-up — treated as "no reason text".
_BOILERPLATE = "to contact the national consumer commission"

# Strip the recall prefix off the title to get the product, e.g. "Product recall – Buttanutt peanut
# butter" → "Buttanutt peanut butter". Handles en-dash, hyphen or colon separators.
_TITLE_PREFIX = re.compile(r"^\s*product\s+(?:safety\s+)?recall\s*[–\-:]\s*", re.IGNORECASE)
# Cap the reason so a long article body can't bloat the row or over-match entities downstream.
_REASON_MAX = 700

# Best-effort company extraction from the body — "notified by X" / "manufacturer, X".
_COMPANY_PATTERNS = (
    re.compile(
        r"(?:notified|informed|communicated|reported)\s+by\s+(?:the\s+supplier,?\s+)?([A-Z][^.,;]{2,70})",
        re.IGNORECASE,
    ),
    re.compile(r"manufacturer,?\s+([A-Z][^.,;]{2,70})", re.IGNORECASE),
)


# The external boundary — a WordPress post, validated by Pydantic and mapped to the domain shape.
class _Rendered(BaseModel):
    model_config = ConfigDict(extra="allow")
    rendered: str = ""


class NccRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = 0
    slug: str = ""
    link: str | None = None
    date: str | None = None
    modified: str | None = None
    title: _Rendered = Field(default_factory=_Rendered)
    content: _Rendered = Field(default_factory=_Rendered)
    excerpt: _Rendered = Field(default_factory=_Rendered)
    categories: list[int] = []
    tags: list[int] = []


def is_food_recall(record: NccRecord) -> bool:
    # A human-food recall: the slug marks it a recall, the deny-list rules out the non-food classes,
    # and a positive food / food-safety signal must be present (default-exclude, since non-food
    # dominates and polluting the food corpus is worse than missing an oddly-worded recall).
    if not record.slug.startswith(_RECALL_SLUG_PREFIXES):
        return False
    text = f"{strip_html(record.title.rendered)} {strip_html(record.content.rendered)}".lower()
    if any(term in text for term in _NON_FOOD_DENY):
        return False
    return any(term in text for term in _FOOD_ALLOW)


def _product_description(record: NccRecord) -> str:
    title = strip_html(record.title.rendered)
    return _TITLE_PREFIX.sub("", title).strip() or title


def _reason_text(record: NccRecord) -> str:
    # Take the article's opening paragraphs (the lede states the product + hazard), splitting before
    # tags are stripped so the trailing product/batch table is left out. A boilerplate-only stub has
    # no real reason, so fall back to the product description (the title carries the food context).
    parts = re.split(r"</p>|</li>|<br\s*/?>", record.content.rendered or "", flags=re.IGNORECASE)
    paragraphs = [p for p in (strip_html(part) for part in parts) if len(p) > 1]
    text = " ".join(paragraphs[:2]).strip()
    if not text or text.lower().startswith(_BOILERPLATE):
        return _product_description(record)
    return text[:_REASON_MAX]


def _company(body: str) -> str | None:
    for pattern in _COMPANY_PATTERNS:
        match = pattern.search(body)
        if match:
            return match.group(1).strip() or None
    return None


def normalize_ncc(record: NccRecord) -> NormalizedRecall:
    product = _product_description(record)
    reason_text = _reason_text(record)
    category, confidence = classify(reason_text)
    entities = extract_entities(reason_text)
    report_date = parse_iso_date(record.date)
    # NCC issues no FDA-style class and no geography, so severity rests on the cause category, named
    # entities, the allergen tier, and any reported harm (classification=None → the category base).
    severity_score, severity_label = score_severity(
        classification=None,
        category=category.value,
        entities=entities,
        reason_text=reason_text,
    )
    return {
        "source": RecallSource.ncc.value,
        "country": RecallCountry.za.value,
        "recall_number": record.slug,  # stable per post; the NCC issues no recall number
        "source_url": record.link,
        "event_id": None,
        "status": None,
        "classification": None,
        "product_description": product,
        "reason_text": reason_text,
        "company_name": _company(strip_html(record.content.rendered)),
        "state": None,
        "states": None,
        "distribution_pattern": None,
        "recall_initiation_date": report_date,
        "report_date": report_date,
        "category": category.value,
        "category_confidence": confidence,
        "severity_score": severity_score,
        "severity_label": severity_label,
        "entities": entities,
        "raw": record.model_dump(),
    }


# Pages the WordPress posts endpoint (X-WP-TotalPages bounds the loop) and keeps the human-food
# recalls. The NCC publishes a few hundred posts total (a few pages), so a full pass is cheap.
def fetch_ncc() -> list[NccRecord]:
    records: list[NccRecord] = []
    page, total_pages = 1, 1
    while page <= total_pages and page <= _MAX_PAGES:
        response = curl_requests.get(
            ENDPOINT,
            params={"per_page": _PAGE, "page": page, "_fields": _FIELDS},
            headers={"Accept": "application/json"},
            impersonate="chrome",
            timeout=60,
        )
        response.raise_for_status()
        if page == 1:
            total_pages = int(response.headers.get("X-WP-TotalPages") or 1)
        items = response.json()
        if not items:
            break
        records.extend(
            record for item in items if is_food_recall(record := NccRecord.model_validate(item))
        )
        page += 1
    return records
