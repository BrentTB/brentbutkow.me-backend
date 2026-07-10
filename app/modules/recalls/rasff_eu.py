"""EU / RASFF source — the Rapid Alert System for Food and Feed (iRASFF).

Two endpoints, by design:

* The **official, keyless, documented** DG SANTE data-lake API is the canonical ingest
  (`irasff-general-info-view`). Everything a recall *is* — subject, dates, countries, hazard,
  classification — comes from here. It is versioned (v1.1) and filterable by date window and
  network, so it drives both the daily job and the one-off history seed.
* The **RASFF Window SPA backend** (undocumented) is a *best-effort enrichment only*. It is the
  only place the per-notification `measures` live — the national food-authority URL and the
  action taken ("recall from consumer"). We attach those when we can and shrug when we can't; a
  RASFF recall is fully valid without them. See scripts/backfill_rasff_enrichment.py for the
  one-off historical pass.

RASFF is ingested as one EU-wide country ("eu"); the member-state detail rides in
notifying_country / origin_countries / distribution_countries (see normalize.py). RASFF carries no
native Class I/II/III ladder — only a risk decision — so `classification` stays None and "eu"
joins PREDICT_COUNTRIES (class_predictor.py), never the training set.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.modules.recalls.classifier import classify
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.normalize import NormalizedRecall, parse_iso_date, strip_html
from app.modules.recalls.schemas import RecallCountry, RecallSource
from app.modules.recalls.severity import score_severity

# The official, keyless data-lake API. `NETWORK_DESC=RASFF` restricts to food/feed alerts — the
# v1.1 endpoint also serves the other iRASFF networks (AAC, Food Fraud, Plant Health, Animal
# Welfare, Pet Animals), which are not food recalls and must be filtered out. Paginates via an
# absolute `nextLink`, 100 rows/page.
API_ENDPOINT = "https://api.datalake.sante.service.ec.europa.eu/rasff/irasff-general-info-view"
API_VERSION = "v1.1"

# The RASFF Window SPA backend — enrichment source AND the deterministic public page for any
# notification (our source_url fallback when a record has no national-authority link). The host is
# also how the enrichment backfill spots a not-yet-upgraded row (source_url still points here).
RASFF_WINDOW_HOST = "webgate.ec.europa.eu"
_SPA_DETAIL = (
    f"https://{RASFF_WINDOW_HOST}/rasff-window/backend/public/notification/view/id/{{id}}/"
)
_SPA_PAGE = f"https://{RASFF_WINDOW_HOST}/rasff-window/screen/notification/{{id}}"

# The feed packs multi-valued fields into one string on this separator (per the API data dict).
_LIST_SEP = "***"

# Which notification classifications we treat as recalls. Border rejections are pre-market
# interceptions (product stopped at the frontier, never distributed to consumers), so they are not
# recalls and are dropped. Alerts and information notifications concern product that may have
# reached the market. This is the classification-based recall filter — it never depends on the
# best-effort enrichment (a batch whose enrichment fails must not silently drop real recalls).
_RECALL_CLASSIFICATIONS = frozenset(
    {
        "alert notification",
        "information notification for attention",
        "information notification for follow-up",
    }
)

# Action descriptions (from enrichment measures) that indicate a genuine recall/withdrawal — used
# only to surface a human-readable status, never to gate ingest.
_RECALL_ACTIONS = ("recall", "withdraw")

# Country name → ISO 3166-1 alpha-2. The official API gives English names only (the SPA gives
# codes). Exhaustive for EU-27 + EFTA + UK + European microstates (every possible notifying /
# distribution country); best-effort for worldwide origin countries — an unmapped name yields None
# and is simply dropped from a list (origins are global, so we never raise on an unknown one).
# NOTE: RASFF uses "GR" for Greece (not the Eurostat "EL"); keep GR, it matches the feed + the SPA.
_NAME_TO_ISO: dict[str, str] = {
    # EU-27
    "Austria": "AT",
    "Belgium": "BE",
    "Bulgaria": "BG",
    "Croatia": "HR",
    "Cyprus": "CY",
    "Czechia": "CZ",
    "Czech Republic": "CZ",
    "Denmark": "DK",
    "Estonia": "EE",
    "Finland": "FI",
    "France": "FR",
    "Germany": "DE",
    "Greece": "GR",
    "Hungary": "HU",
    "Ireland": "IE",
    "Italy": "IT",
    "Latvia": "LV",
    "Lithuania": "LT",
    "Luxembourg": "LU",
    "Malta": "MT",
    "Netherlands": "NL",
    "Poland": "PL",
    "Portugal": "PT",
    "Romania": "RO",
    "Slovakia": "SK",
    "Slovenia": "SI",
    "Spain": "ES",
    "Sweden": "SE",
    # EFTA + UK + European microstates / neighbours that appear as notifying/distribution
    "Iceland": "IS",
    "Liechtenstein": "LI",
    "Norway": "NO",
    "Switzerland": "CH",
    "United Kingdom": "GB",
    "United Kingdom (Northern Ireland)": "GB",
    "Andorra": "AD",
    "Monaco": "MC",
    "San Marino": "SM",
    "Albania": "AL",
    "Bosnia and Herzegovina": "BA",
    "Montenegro": "ME",
    "North Macedonia": "MK",
    "Serbia": "RS",
    "Türkiye": "TR",
    "Turkey": "TR",
    "Ukraine": "UA",
    "Moldova": "MD",
    "Kosovo": "XK",
    # Common worldwide origin countries (best-effort; extend freely, unknowns just drop)
    "China": "CN",
    "United States": "US",
    "Brazil": "BR",
    "India": "IN",
    "Thailand": "TH",
    "Vietnam": "VN",
    "South Korea": "KR",
    "Japan": "JP",
    "Egypt": "EG",
    "Nigeria": "NG",
    "Morocco": "MA",
    "Tunisia": "TN",
    "Argentina": "AR",
    "Chile": "CL",
    "Peru": "PE",
    "Mexico": "MX",
    "Canada": "CA",
    "Australia": "AU",
    "New Zealand": "NZ",
    "Indonesia": "ID",
    "Malaysia": "MY",
    "Philippines": "PH",
    "Pakistan": "PK",
    "Bangladesh": "BD",
    "Sri Lanka": "LK",
    "Israel": "IL",
    "Iran": "IR",
    "South Africa": "ZA",
    "Ghana": "GH",
    "Kenya": "KE",
    "Ecuador": "EC",
    "Colombia": "CO",
    "Costa Rica": "CR",
    "Dominican Republic": "DO",
    "Taiwan": "TW",
    "Hong Kong": "HK",
    "United Arab Emirates": "AE",
    "Saudi Arabia": "SA",
    "Russia": "RU",
}


class RasffRecord(BaseModel):
    """One row of the official general-info-view, plus the optional enrichment we attach.

    Enrichment fields are populated by `enrich_records` (best-effort, network I/O) *before* the
    row reaches `normalize_rasff`, keeping the normalizer pure and fixture-testable. `extra=allow`
    keeps any future feed fields in `raw`.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    notif_id: int | None = Field(default=None, alias="NOTIF_ID")
    reference: str | None = Field(default=None, alias="NOTIFICATION_REFERENCE")
    notif_date: str | None = Field(default=None, alias="NOTIF_DATE")
    status_desc: str | None = Field(default=None, alias="NOTIFICATION_STATUS_DESC")
    product_name: str | None = Field(default=None, alias="PRODUCT_NAME")
    product_category: str | None = Field(default=None, alias="PRODUCT_CATEGORY_DESC")
    notification_type: str | None = Field(default=None, alias="NOTIFICATION_TYPE_DESC")
    subject: str | None = Field(default=None, alias="NOTIF_SUBJECT")
    notifying_country: str | None = Field(default=None, alias="NOTIFYNG_COUNTRY_DESC")
    classification: str | None = Field(default=None, alias="NOTIFICATION_CLASSIFICAT_DESC")
    basis: str | None = Field(default=None, alias="NOTIFICATION_BASIS_DESC")
    risk_decision: str | None = Field(default=None, alias="RISK_DECISION_DESC")
    hazards: str | None = Field(default=None, alias="HAZARD_CATEGORY_NAME")
    origin_countries: str | None = Field(default=None, alias="ORIGIN_COUNTRY_DESC")
    distribution_countries: str | None = Field(default=None, alias="DISTRIBUTION_COUNTRY_DESC")
    distribution_status: str | None = Field(default=None, alias="DISTRIBUTION_STATUS_DESC")
    network: str | None = Field(default=None, alias="NETWORK_DESC")

    # Enrichment — set by enrich_records(); never present in the official feed.
    enriched_url: str | None = None
    enriched_actions: list[str] = Field(default_factory=list)
    enrichment_attempted: bool = False


def _iso(name: str | None) -> str | None:
    """Map one country name to its ISO alpha-2 code, or None when unknown/blank."""
    if not name:
        return None
    return _NAME_TO_ISO.get(name.strip())


def _iso_list(raw: str | None) -> list[str] | None:
    """Split a ``***``-separated country-name list into de-duplicated ISO codes (order-preserving).

    Returns None for an absent/empty field (the ``states`` none_as_null contract), so an empty
    distribution list — a border-stopped product — reads as "no known distribution", not "[]".
    """
    if not raw:
        return None
    codes: list[str] = []
    for part in raw.split(_LIST_SEP):
        code = _iso(part)
        if code and code not in codes:
            codes.append(code)
    return codes or None


def _hazard_text(raw: str | None) -> str:
    """Flatten the ``***``-separated ``NAME - {CATEGORY}`` hazard list into a plain phrase."""
    if not raw:
        return ""
    names: list[str] = []
    for part in raw.split(_LIST_SEP):
        name = part.split(" - {", 1)[0].strip()
        name = " ".join(name.split())  # collapse the feed's doubled internal spaces
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


def is_recall(record: RasffRecord) -> bool:
    """Classification-based recall filter (see _RECALL_CLASSIFICATIONS). Enrichment-independent."""
    return (record.classification or "").strip().lower() in _RECALL_CLASSIFICATIONS


def _status(record: RasffRecord) -> str | None:
    """Human-readable status: the recall/withdrawal actions when enrichment found them.

    The action list is the truest recall signal, but it comes from the best-effort enrichment, so
    it only ever *adds* detail — it never gates ingest. Absent enrichment, there is no lifecycle
    field in the summary feed worth surfacing, so status is None.
    """
    actions = [a for a in record.enriched_actions if a]
    recall_actions = [a for a in actions if any(k in a.lower() for k in _RECALL_ACTIONS)]
    chosen = recall_actions or actions
    return "; ".join(dict.fromkeys(chosen)) or None


def normalize_rasff(record: RasffRecord) -> NormalizedRecall:
    # The notification reference is the upsert key; an empty one would collide on the composite PK.
    # fetch_rasff already drops reference-less rows, so this guards a future direct caller.
    if not record.reference:
        raise ValueError("RASFF record has no NOTIFICATION_REFERENCE — the upsert key is missing")

    subject = strip_html(record.subject)
    hazard = _hazard_text(record.hazards)
    product = strip_html(record.product_name)
    # The subject is a complete English sentence (hazard + product + origin); it's the best reason
    # text and classifier input. Fall back to the hazard/product when a row somehow lacks one.
    reason_text = subject or (f"{hazard} in {product}".strip() if hazard or product else "")
    # Product line for digests. RASFF withholds brand/company from the public view, so this is a
    # generic product name — fall back to the subject so a line is never empty.
    product_description = product or subject or (record.reference or "")

    category, confidence = classify(reason_text)
    entities = extract_entities(f"{reason_text} {hazard}".strip())
    notif_date = parse_iso_date(record.notif_date)
    # RASFF has no native class ladder (only a risk decision), so classification stays None and the
    # class predictor fills predicted_class for "eu". Severity leans on the cause/entities the same
    # way the UK/CA paths do (no firm or class to draw on).
    severity_score, severity_label = score_severity(
        classification=None,
        category=category.value,
        entities=entities,
        reason_text=reason_text,
    )
    # National food-authority link when enrichment found one; otherwise the deterministic public
    # RASFF Window page — every notification has one, so source_url is never null.
    source_url = record.enriched_url or (
        _SPA_PAGE.format(id=record.notif_id) if record.notif_id else None
    )
    return {
        "source": RecallSource.rasff.value,
        "country": RecallCountry.eu.value,
        "recall_number": record.reference,
        "source_url": source_url,
        "event_id": str(record.notif_id) if record.notif_id else None,
        "status": _status(record),
        # No Class I/II/III in RASFF — left to the class predictor (see module docstring).
        "classification": None,
        "product_description": product_description,
        "reason_text": reason_text,
        # RASFF's public view withholds the operator, so there is no company for the leaderboard.
        "company_name": None,
        # EU-wide source: no US state. Member-state geography rides in the *_countries fields.
        "state": None,
        "states": None,
        "notifying_country": _iso(record.notifying_country),
        "origin_countries": _iso_list(record.origin_countries),
        "distribution_countries": _iso_list(record.distribution_countries),
        # The distribution status is a readable phrase ("no distribution from notifying country").
        "distribution_pattern": strip_html(record.distribution_status) or None,
        "recall_initiation_date": notif_date,
        "report_date": notif_date,
        "category": category.value,
        "category_confidence": confidence,
        "severity_score": severity_score,
        "severity_label": severity_label,
        "entities": entities,
        "raw": record.model_dump(mode="json", by_alias=True, exclude_none=True),
    }


def _enrich_one(record: RasffRecord, client: httpx.Client) -> None:
    """Best-effort: attach the national-authority URL + action list from the SPA detail payload.

    Any failure (timeout, 404, schema drift, endpoint gone) is swallowed by the caller — the
    record stays valid on its official-API fields alone.
    """
    # Observed latency is ~1-1.5s per call (server-side); 8s already means something is wrong.
    response = client.get(_SPA_DETAIL.format(id=record.notif_id), timeout=8)
    response.raise_for_status()
    product = response.json().get("product") or {}
    # The primary product's measures, then any related products' — an audit over the live corpus
    # found ~4% of no-URL notifications carry their only authority link under relatedProducts
    # (everything else URL-shaped in the payload is generic country alert pages or shop listings).
    measures = list(product.get("measures") or [])
    for related in product.get("relatedProducts") or []:
        measures.extend((related or {}).get("measures") or [])
    actions: list[str] = []
    recall_url: str | None = None
    first_url: str | None = None
    for measure in measures:
        action = ((measure.get("actionTaken") or {}).get("description") or "").strip()
        if action:
            actions.append(action)
        url = measure.get("url")
        if url:
            first_url = first_url or url
            if any(keyword in action.lower() for keyword in _RECALL_ACTIONS):
                recall_url = recall_url or url
    record.enriched_url = recall_url or first_url
    record.enriched_actions = actions
    record.enrichment_attempted = True


def _try_enrich(record: RasffRecord, client: httpx.Client) -> bool:
    try:
        _enrich_one(record, client)
        return True
    except (httpx.HTTPError, ValueError, KeyError):
        return False


def enrich_records(records: list[RasffRecord], *, workers: int = 4) -> int:
    """Enrich a batch in place, best-effort. Returns how many rows were successfully enriched.

    The SPA detail endpoint costs ~1s per call server-side regardless of outcome, so a sequential
    pass over the full history would take hours — a small worker pool bounds the wall-clock while
    staying polite (politeness = bounded concurrency, not pacing sleeps; the connection pool is
    capped to the worker count so the endpoint never sees more than `workers` in flight). Each row
    is independent: one failure never aborts the batch, and a wholesale SPA outage yields zero
    enrichments. httpx.Client is thread-safe, so the workers share one connection pool.
    """
    todo = [record for record in records if record.notif_id is not None]
    if not todo:
        return 0
    limits = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    with (
        httpx.Client(follow_redirects=True, limits=limits) as client,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        return sum(pool.map(lambda record: _try_enrich(record, client), todo))


def fetch_rasff(*, days: int | None = 14, enrich: bool = True) -> list[RasffRecord]:
    """Fetch RASFF notifications from the official API, newest window first, following nextLink.

    `days` bounds the ingest to notifications created in the last N days (the daily cursor window);
    None fetches the entire public history (2020+) for the one-off seed. Only recall-classified
    rows are kept (border rejections dropped). When `enrich`, each kept row gets a best-effort SPA
    enrichment pass.
    """
    # The query is hand-built rather than passed as httpx params: the datalake API 400s on a
    # percent-encoded timestamp (NOTIF_DATE_FROM=…T00%3A00%3A00Z), so the colons must go over the
    # wire raw. httpx preserves a pre-built URL string but would %-encode a params dict. Every
    # value here is a URL-safe literal, so no other encoding is needed.
    query = f"format=json&api-version={API_VERSION}&NETWORK_DESC=RASFF"
    if days is not None:
        since = datetime.now(UTC) - timedelta(days=days)
        query += f"&NOTIF_DATE_FROM={since.strftime('%Y-%m-%dT00:00:00Z')}"

    records: list[RasffRecord] = []
    with httpx.Client(follow_redirects=True) as client:
        url: str | None = f"{API_ENDPOINT}?{query}"
        while url:
            # nextLink is fully-formed (it carries the API's own cursor), so later pages use it
            # verbatim.
            response = client.get(url, timeout=120)
            response.raise_for_status()
            payload = response.json()
            for item in payload.get("value", []):
                record = RasffRecord.model_validate(item)
                # Defence in depth: the query already filters to RASFF, but never trust a feed to
                # honour it. Keep only RASFF-network, recall-classified rows with an upsert key.
                if record.reference and record.network == "RASFF" and is_recall(record):
                    records.append(record)
            url = payload.get("nextLink")

    if enrich:
        enrich_records(records)
    return records
