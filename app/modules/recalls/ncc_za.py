"""South Africa recall source — the National Consumer Commission (NCC).

South Africa has no recall API. The NCC publishes recall notices on its WordPress site, so the
practical "API" is the WordPress REST API (`/wp-json/wp/v2/posts`): each post is JSON carrying the
article HTML (`content.rendered`), an ISO `date`, and a stable `slug` we use as the identifier.
That's structured and theme-independent — far less brittle than scraping rendered pages.

Two NCC-specific problems shaped this module:

* **Mixed product types, two slug conventions.** The NCC recalls cars, tyres, electronics, cosmetics
  and food, under both `product-recall-*` / `product-safety-recall-*` slugs *and* `media-statement-…
  recall…` posts (where many retailer-initiated food recalls live, with no dedicated post). Recall
  Radar is a *food* platform, so `is_food_recall` keeps human food only: a non-food deny-list
  on the *title* (the product class is always named there, so an incidental word in a long statement
  can't misfire) plus a food / food-safety allow-list matched on the title or body. `fetch_ncc`
  dedupes a recall that appears under both slug conventions.
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

# Recall posts use two slug conventions: dedicated product-recall-* / product-safety-recall-* posts,
# and "media-statement-…recall…" posts (where many retailer-initiated food recalls live).
_RECALL_SLUG_PREFIXES = ("product-recall-", "product-safety-recall-")

# Recall *lifecycle* posts that aren't a single product recall — a closure ("…closes…recall…") or a
# periodic digest listing many recalls. Skipped: the individual recalls have their own posts.
_SKIP_SLUG_MARKERS = (
    "closes",
    "closed",
    "administered-during-quarter",
    "notice-update",
    "notified-product-recalls",
    "product-recall-notifications",
)

# NCC also publishes some recalls under a bare "consumers-are-urged-to-return-…" /
# "…urges-consumers-to-return-…" slug (no product-recall- / media-statement- prefix) — e.g. the 2021
# Appletiser and apple-juice recalls. _canonical collapses any that also have a prefixed twin.
_RECALL_SLUG_MARKERS = ("urged-to-return", "urges-consumers-to-return")

# Non-food classes the NCC also recalls — matched against the TITLE (the product class is named
# there). Listed first so a non-food recall can't slip through on a stray food word: "dry pet foods"
# contains "food", a "honey"-scented shampoo contains "honey".
_NON_FOOD_TERMS = (
    "pet food",
    "pet foods",
    "dry pet",
    "dog food",
    "dog foods",
    "cat food",
    "cat foods",
    "dog and cat",
    "animal feed",
    "vehicle",
    "truck",
    "tyre",
    "tire",
    "motorcycle",
    "sedan",
    "suv",
    "bakkie",
    "power bank",
    "charger",
    "charging",
    "kettle",
    "electrode",
    "medical device",
    "stent",
    "catheter",
    "battery",
    "cable",
    "adapter",
    "appliance",
    "laptop",
    "wireless",
    "headphone",
    "earbud",
    "spray paint",
    "galvanising",
    "ladder",
    "shampoo",
    "relaxer",
    "cosmetic",
    "sunscreen",
    "perfume",
    "makeup",
    "lotion",
    "deodorant",
    "toothpaste",
    "soap",
    "detergent",
    "bleach",
    "disinfectant",
    "cleaner",
    "shea butter",
    "body butter",
    "helmet",
    "boxing",
    "basketball",
    "hoop",
    "dumbbell",
    "treadmill",
    "bicycle",
    "scooter",
    "stroller",
    "pram",
    "toy",
    "toys",
    "furniture",
    "mattress",
)

# Positive human-food signals: food/drink nouns plus the food-safety vocabulary that only appears in
# food recalls. A kept post must match one of these in its title or body.
_FOOD_TERMS = (
    "peanut",
    "infant formula",
    "formula",
    "baby food",
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
    "use by",
    "ready-to-eat",
    "ingredient",
    "food",
    "food safety",
    "foodborne",
    "microbiological",
    "mould",
    "aflatoxin",
    "listeria",
    "listeriosis",
    "salmonella",
    "cereulide",
    "botulism",
    "norovirus",
    "hepatitis",
    "undeclared allergen",
    "hummus",
    "porridge",
    "maize",
    "stock cube",
    "bouillon",
    "milk",
    "cheese",
    "yoghurt",
    "yogurt",
    "margarine",
    "butter",
    "bread",
    "flour",
    "rice",
    "pasta",
    "noodle",
    "sauce",
    "mayonnaise",
    "jam",
    "honey",
    "sweets",
    "biscuit",
    "cookie",
    "chips",
    "crisps",
    "dried fruit",
    "ice cream",
    "dairy",
    "drink",
    "meat",
    "beef",
    "pork",
    "chicken",
    "poultry",
    "fish",
    "seafood",
    "tuna",
    "pilchard",
    "sardine",
    "sausage",
    "polony",
    "vienna",
    "biltong",
    "egg",
)

_FOOD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _FOOD_TERMS) + r")\b", re.IGNORECASE
)
_NON_FOOD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _NON_FOOD_TERMS) + r")\b", re.IGNORECASE
)

# Placeholder body some recalls carry instead of a write-up — treated as "no reason text".
_BOILERPLATE = "to contact the national consumer commission"

# Strip recall boilerplate off the title to get the product, for both the dedicated-post style
# ("Product recall – Buttanutt peanut butter") and media-statement style ("Media Statement: Deli
# Hummus range Product Safety Recall"): drop a leading "Media Statement:" / "Product recall:"
# / "Recall of", and a trailing "… Product Safety Recall".
_TITLE_PREFIX = re.compile(
    r"^\s*(?:media\s+statements?\s*[:–\-]\s*)?"
    r"(?:product\s+(?:safety\s+)?recall\s*[:–\-]\s*|recalls?\s+of\s+(?:the\s+)?)?",
    re.IGNORECASE,
)
_TITLE_SUFFIX = re.compile(r"\s*[:–\-]?\s*product\s+(?:safety\s+)?recall\s*$", re.IGNORECASE)
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


def _is_recall_post(slug: str) -> bool:
    # A single product recall: a dedicated product-recall-* post, or a media-statement whose slug
    # names a recall. Lifecycle posts (closures, periodic digests) are not individual recalls.
    if any(marker in slug for marker in _SKIP_SLUG_MARKERS):
        return False
    if slug.startswith(_RECALL_SLUG_PREFIXES):
        return True
    if slug.startswith("media-statement") and "recall" in slug:
        return True
    return any(marker in slug for marker in _RECALL_SLUG_MARKERS)


def is_food_recall(record: NccRecord) -> bool:
    # Keep human-food recalls only. The deny-list matches the TITLE (the product class is named
    # there) so an incidental non-food word in a long media statement can't drop a real food recall;
    # the food allow-list then matches the title OR body (a hazard like "cereulide" can sit deep in
    # the article). Default-exclude: non-food dominates, so polluting the corpus is worse than
    # missing an oddly-worded recall.
    if not _is_recall_post(record.slug):
        return False
    title = strip_html(record.title.rendered)
    if _NON_FOOD_RE.search(title):
        return False
    return bool(_FOOD_RE.search(f"{title} {strip_html(record.content.rendered)}"))


def _product_description(record: NccRecord) -> str:
    title = strip_html(record.title.rendered)
    return _TITLE_SUFFIX.sub("", _TITLE_PREFIX.sub("", title)).strip() or title


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


# Strip a slug to the recalled subject, so a recall posted under both a product-recall-* and a
# media-statement-* slug collapses to one record.
_CANONICAL_PREFIX = re.compile(
    r"^(?:product-safety-recall-|product-recall-"
    r"|the-ncc-urges-consumers-to-return-|ncc-urges-consumers-to-return-(?:certain-)?"
    r"|consumers-are-urged-to-return-(?:certain-|recalled-)?"
    r"|recalls-of-the-|recalls-of-|recall-of-the-|recall-of-|recalls-|recall-)"
)


def _canonical(slug: str) -> str:
    core = slug
    for prefix in ("media-statements-", "media-statement-"):
        if core.startswith(prefix):
            core = core[len(prefix) :]
            break
    # Strip layered prefixes, e.g. "product-recall-the-ncc-urges-consumers-to-return-similac-…".
    prev = ""
    while prev != core:
        prev, core = core, _CANONICAL_PREFIX.sub("", core)
    return core


def _dedupe(records: list[NccRecord]) -> list[NccRecord]:
    # Prefer the dedicated product-recall-* post over a media-statement twin for the same recall.
    chosen: dict[str, NccRecord] = {}
    for record in sorted(records, key=lambda r: not r.slug.startswith(_RECALL_SLUG_PREFIXES)):
        chosen.setdefault(_canonical(record.slug), record)
    return list(chosen.values())


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
    return _dedupe(records)
