"""South Africa — curated recalls the NCC feed doesn't carry.

A small, hand-maintained set of food recalls that don't appear on the NCC site, so the NCC scraper
can't reach them:

* **Woolworths own-brand recalls** — Woolworths Holdings is on WordPress, but its REST API is
  locked and the pages render reason text in nav-heavy HTML, so reasons can't be scraped cleanly.
  The sitemap lists the recall URLs; the reasons here are transcribed from the public notices.
* **A Shoprite/Checkers recall** the NCC didn't post individually (most Shoprite recalls *do* appear
  on NCC as media statements — only this one didn't).
* **The two NRCS canned-fish recalls** — the NRCS recalls page is JavaScript-only and returns no
  content to an HTTP client, so it isn't scrapeable.
* **The 2017-18 Tiger Brands / Enterprise listeriosis recall** — administered by the NCC but
  predating its web archive; SA's largest-ever food recall (the world's largest listeriosis
  outbreak). Attributed to source "ncc" since the NCC ordered it.

Each entry keeps its **true source** (so the by-source breakdown stays honest) and the URL it was
taken from. New SA recalls still arrive automatically via the NCC source; this only fills the gaps
NCC doesn't cover, updated by hand when a notable retailer/regulator recall surfaces.
"""

from typing import TypedDict

from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.normalize import NormalizedRecall, parse_iso_date
from app.modules.recalls.schemas import RecallCountry, RecallSource
from app.modules.recalls.severity import score_severity


class SeedRecall(TypedDict):
    source: str  # a RecallSource value — the recall's true origin
    slug: str  # stable identifier (becomes recall_number)
    report_date: str  # YYYY-MM-DD (best-known; NRCS dates are month-level)
    product: str
    reason: str
    company: str | None
    url: str


# Reasons are phrased to read naturally *and* to surface the right entity from the gazetteer
# (e.g. "milk" → allergen, "patulin"/"aflatoxin" → contaminant, "Listeria" → pathogen).
_SEED: list[SeedRecall] = [
    {
        "source": RecallSource.woolworths.value,
        "slug": "woolworths-peanut-butter-dairy-ice-cream",
        "report_date": "2024-02-23",
        "product": "Woolworths Peanut Butter Dairy Ice Cream",
        "reason": (
            "Recalled because aflatoxin levels exceeded the legal limit. Aflatoxin is a "
            "mould-produced toxin linked to liver damage."
        ),
        "company": "Woolworths",
        "url": "https://www.woolworthsholdings.co.za/product-recall-woolworths-peanut-butter-dairy-ice-cream/",
    },
    {
        "source": RecallSource.woolworths.value,
        "slug": "woolworths-chicken-viennas",
        "report_date": "2023-05-19",
        "product": "Woolworths Chicken Viennas (smoked, cocktail, halaal smoked)",
        "reason": (
            "Recalled because a production-line fault cross-contaminated the chicken viennas with "
            "undeclared milk protein, a risk to consumers with a cow's-milk allergy."
        ),
        "company": "Woolworths",
        "url": "https://www.woolworthsholdings.co.za/woolworths-holdings-chicken-vienna-product-recall/",
    },
    {
        "source": RecallSource.woolworths.value,
        "slug": "woolworths-100-apple-juice-200ml-cartons",
        "report_date": "2021-10-09",
        "product": "Woolworths 100% Apple Juice 200ml cartons",
        "reason": (
            "Recalled over elevated patulin — a toxin produced by mould on rotting apples — above "
            "the 50 ppb regulatory limit; traced to apple-juice concentrate from Elgin Fruit Juice."
        ),
        "company": "Woolworths",
        "url": "https://www.woolworthsholdings.co.za/product-recall-woolworths-100-apple-juice-200ml-cartons/",
    },
    {
        "source": RecallSource.woolworths.value,
        "slug": "woolworths-frozen-savoury-rice-mix",
        "report_date": "2018-07-10",
        "product": "Woolworths Frozen Savoury Rice Mix",
        "reason": (
            "Precautionary recall over possible Listeria, linked to frozen sweetcorn from the "
            "Greenyard plant in Hungary implicated in a European listeriosis outbreak."
        ),
        "company": "Woolworths",
        "url": "https://www.woolworthsholdings.co.za/product-recall-frozen-savoury-rice-mix/",
    },
    {
        "source": RecallSource.woolworths.value,
        "slug": "woolworths-ice-cream-sorbet-peanut-allergen",
        "report_date": "2015-10-13",
        "product": "Woolworths ice-cream and sorbet products (12 lines)",
        "reason": (
            "Recalled because the packaging carried inconsistent peanut allergen labelling, a risk "
            "to consumers with a peanut allergy."
        ),
        "company": "Woolworths",
        "url": "https://www.woolworthsholdings.co.za/woolworths-product-recall/",
    },
    {
        "source": RecallSource.shoprite.value,
        "slug": "cape-point-light-meat-shredded-tuna-170g",
        "report_date": "2022-05-27",
        "product": "Cape Point Light Meat Shredded Tuna in Water 170g",
        "reason": (
            "Precautionary recall by Shoprite and Checkers because some cans had defective double "
            "seams, which can compromise the seal."
        ),
        "company": "Shoprite Checkers",
        "url": "https://www.shopriteholdings.co.za/articles/Newsroom/2022/voluntary-recall-cape-point-light-meat-shredded-tuna-in-water.html",
    },
    {
        "source": RecallSource.nrcs.value,
        "slug": "nrcs-canned-pilchards-tomato-chilli-sauce-400g",
        "report_date": "2023-07-01",
        "product": "Canned pilchards in tomato & chilli sauce, 400g (batches ZST29/ZSC29)",
        "reason": (
            "The NRCS ordered the removal of these canned pilchards after an investigation found a "
            "deficiency in the canning process."
        ),
        "company": None,
        "url": "https://www.nrcs.org.za/recalls",
    },
    {
        "source": RecallSource.nrcs.value,
        "slug": "nrcs-canned-molluscs",
        "report_date": "2024-05-01",
        "product": "Canned molluscs",
        "reason": (
            "The NRCS announced a voluntary recall of canned molluscs after a defect was detected "
            "in the canned product."
        ),
        "company": None,
        "url": "https://www.nrcs.org.za/recalls",
    },
    {
        "source": RecallSource.ncc.value,  # NCC-administered; predates its web archive
        "slug": "enterprise-foods-listeriosis-recall-2018",
        "report_date": "2018-03-04",
        "product": "Enterprise ready-to-eat processed meats (polony, viennas, russians)",
        "reason": (
            "South Africa's listeriosis outbreak — the world's largest — was traced to "
            "ready-to-eat processed meats from Tiger Brands' Enterprise Foods facility in "
            "Polokwane. Listeria monocytogenes caused over 1 000 cases and more than 200 "
            "deaths; the NCC ordered a nationwide recall in March 2018."
        ),
        "company": "Tiger Brands (Enterprise Foods)",
        "url": (
            "https://www.gcis.gov.za/newsroom/media-releases/"
            "government-update-listeria-outbreak-joint-media-statement"
        ),
    },
]


def fetch_seed() -> list[SeedRecall]:
    # No network — the curated list IS the source. Shaped like the other `fetch_*` so the generic
    # ingest job (which does fetch() then normalize() per item) drives it unchanged.
    return list(_SEED)


def normalize_seed(entry: SeedRecall) -> NormalizedRecall:
    reason_text = entry["reason"]
    category, confidence = classify(reason_text)
    entities = extract_entities(reason_text)
    # No regulator classification or geography (same as NCC), so severity rests on cause + entities.
    severity_score, severity_label = score_severity(
        classification=None,
        category=category.value,
        entities=entities,
        reason_text=reason_text,
    )
    report_date = parse_iso_date(entry["report_date"])
    return {
        "source": entry["source"],
        "country": RecallCountry.za.value,
        "recall_number": entry["slug"],
        "source_url": entry["url"],
        "event_id": None,
        "status": None,
        "classification": None,
        "product_description": entry["product"],
        "reason_text": reason_text,
        "company_name": entry["company"],
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
        "raw": dict(entry),
    }
