import httpx
import pytest

from app.modules.recalls import rasff_eu, service
from app.modules.recalls.rasff_eu import (
    RASFF_WINDOW_HOST,
    RasffRecord,
    enrich_records,
    fetch_rasff,
    is_recall,
    normalize_rasff,
)
from app.modules.recalls.schemas import RecallCategory, RecallStats

# Trimmed verbatim from the official DG SANTE data-lake API (irasff-general-info-view, v1.1). An
# alert whose distribution list mixes EU members with non-European territories (Cayman Islands,
# Congo, Marshall Islands, Saint Martin) that have no ISO mapping — they must be dropped, not error.
ALERT = {
    "NOTIF_ID": 856736,
    "NOTIFICATION_REFERENCE": "2026.5974",
    "NOTIF_DATE": "2026-07-06T00:00:00",
    "NOTIFICATION_STATUS_DESC": "ec_validated",
    "PRODUCT_NAME": "Saint Marcellin IGP",
    "PRODUCT_CATEGORY_DESC": "milk and milk products",
    "NOTIFICATION_TYPE_DESC": "food",
    "NOTIF_SUBJECT": "DLC issue on cheese labelling ",
    "NOTIFYNG_COUNTRY_DESC": "France",
    "NOTIFICATION_CLASSIFICAT_DESC": "alert notification",
    "NOTIFICATION_BASIS_DESC": "company's own check",
    "RISK_DECISION_DESC": "potentially serious",
    "HAZARD_CATEGORY_NAME": None,
    "ORIGIN_COUNTRY_DESC": "France",
    "DISTRIBUTION_COUNTRY_DESC": (
        "Belgium *** Cayman Islands *** Congo *** Germany *** Italy *** Malta *** "
        "Marshall Islands *** Monaco *** Netherlands *** Poland *** Saint Martin"
    ),
    "DISTRIBUTION_STATUS_DESC": "distribution to other member countries",
    "NETWORK_DESC": "RASFF",
}

# A real border-rejection row — product stopped at the frontier, no distribution. Must be filtered
# out as a non-recall. Its subject has a leading tab and its hazard has the feed's doubled spaces.
BORDER = {
    "NOTIF_ID": 856678,
    "NOTIFICATION_REFERENCE": "2026.6001",
    "NOTIF_DATE": "2026-07-07T00:00:00",
    "NOTIFICATION_STATUS_DESC": "ec_validated",
    "PRODUCT_NAME": "Aves",
    "PRODUCT_CATEGORY_DESC": "poultry meat and poultry meat products",
    "NOTIFICATION_TYPE_DESC": "food",
    "NOTIF_SUBJECT": "\tSalmonella spp. in poultry meat preparation from Brazil.",
    "NOTIFYNG_COUNTRY_DESC": "Netherlands",
    "NOTIFICATION_CLASSIFICAT_DESC": "border rejection notification",
    "NOTIFICATION_BASIS_DESC": "border control - consignment detained",
    "RISK_DECISION_DESC": "serious",
    "HAZARD_CATEGORY_NAME": "Salmonella spp.  - {pathogenic micro-organisms}",
    "ORIGIN_COUNTRY_DESC": "Brazil",
    "DISTRIBUTION_COUNTRY_DESC": None,
    "DISTRIBUTION_STATUS_DESC": "no distribution from notifying country",
    "NETWORK_DESC": "RASFF",
}

# A real RASFF Window detail payload (notification 2026.6073) — a French recall whose national link
# lives on the "recall from consumer" measure, not the "withdrawal from the market" one.
DETAIL = {
    "product": {
        "measures": [
            {
                "takenBy": {"organizationName": "France", "isoCode": "FR"},
                "actionTaken": {"description": "withdrawal from the market"},
            },
            {
                "takenBy": {"organizationName": "France", "isoCode": "FR"},
                "url": "rappel.conso.gouv.fr/fiche-rappel/22514/Interne",
                "actionTaken": {"description": "recall from consumer"},
            },
        ]
    }
}


@pytest.fixture(autouse=True)
def _stub_classifier(monkeypatch):
    # Isolate normalization from the ML classifier, like every other feed's tests.
    monkeypatch.setattr(rasff_eu, "classify", lambda _text: (RecallCategory.other, 0.5))


def _normalize(raw):
    return normalize_rasff(RasffRecord.model_validate(raw))


def _mock_httpx(monkeypatch, handler):
    # Route every httpx.Client through a MockTransport. Capture the real class first — after the
    # patch, referencing httpx.Client inside the factory would resolve to the factory itself.
    real_client = httpx.Client

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def test_alert_maps_countries_to_iso_dropping_unknowns():
    row = _normalize(ALERT)
    assert row["source"] == "rasff"
    assert row["country"] == "eu"
    assert row["recall_number"] == "2026.5974"
    assert row["notifying_country"] == "FR"
    assert row["origin_countries"] == ["FR"]
    # EU members + Monaco kept in feed order; the four non-European territories with no ISO mapping
    # are dropped rather than raising.
    assert row["distribution_countries"] == ["BE", "DE", "IT", "MT", "MC", "NL", "PL"]
    assert row["company_name"] is None
    assert row["classification"] is None  # RASFF has no native class ladder
    assert row["state"] is None and row["states"] is None


def test_border_rejection_is_not_a_recall():
    assert is_recall(RasffRecord.model_validate(ALERT)) is True
    assert is_recall(RasffRecord.model_validate(BORDER)) is False


def test_empty_distribution_normalizes_to_none_not_empty_list():
    # A border-stopped product has no distribution; the field must be None (the states none_as_null
    # contract), so it reads as "no known distribution" rather than an empty array.
    row = _normalize(BORDER)
    assert row["distribution_countries"] is None
    assert row["origin_countries"] == ["BR"]


def test_hazard_and_subject_whitespace_is_collapsed():
    # The subject's leading tab and the hazard's doubled internal spaces must be flattened.
    row = _normalize(BORDER)
    assert row["reason_text"] == "Salmonella spp. in poultry meat preparation from Brazil."
    assert not row["reason_text"].startswith((" ", "\t"))


def test_missing_reference_raises():
    with pytest.raises(ValueError, match="NOTIFICATION_REFERENCE"):
        _normalize({**ALERT, "NOTIFICATION_REFERENCE": None})


def test_source_url_falls_back_to_rasff_window_page():
    # With no enrichment, source_url is the deterministic public page (never null).
    row = _normalize(ALERT)
    assert row["source_url"] == (
        "https://webgate.ec.europa.eu/rasff-window/screen/notification/856736"
    )
    assert row["event_id"] == "856736"


def test_enrichment_attaches_national_url_and_actions(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DETAIL)

    _mock_httpx(monkeypatch, handler)
    record = RasffRecord.model_validate(ALERT)
    assert enrich_records([record]) == 1
    # Prefers the recall measure's URL over the withdrawal measure's (which had none here).
    assert record.enriched_url == "rappel.conso.gouv.fr/fiche-rappel/22514/Interne"
    assert record.enrichment_attempted is True

    row = normalize_rasff(record)
    assert row["source_url"] == "rappel.conso.gouv.fr/fiche-rappel/22514/Interne"
    # Both measures are recall-type actions (withdrawal + recall), surfaced as status in feed order.
    assert row["status"] == "withdrawal from the market; recall from consumer"


def test_enrichment_pool_counts_successes_and_skips_failures(monkeypatch):
    # The worker pool must enrich every record with a notif_id, tolerate per-record failures
    # without aborting the batch, and report only the successes — same contract as the old
    # sequential loop, now concurrent (the full history at ~1s/call is hours sequentially).
    def handler(request: httpx.Request) -> httpx.Response:
        if "/id/2/" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=DETAIL)

    _mock_httpx(monkeypatch, handler)
    records = [
        RasffRecord.model_validate({**ALERT, "NOTIF_ID": 1, "NOTIFICATION_REFERENCE": "r.1"}),
        RasffRecord.model_validate({**ALERT, "NOTIF_ID": 2, "NOTIFICATION_REFERENCE": "r.2"}),
        RasffRecord.model_validate({**ALERT, "NOTIF_ID": 3, "NOTIFICATION_REFERENCE": "r.3"}),
        # No notif_id → nothing to fetch; must be skipped, not crash a worker.
        RasffRecord.model_validate({**ALERT, "NOTIF_ID": None, "NOTIFICATION_REFERENCE": "r.4"}),
    ]
    assert enrich_records(records, workers=3) == 2
    assert records[0].enriched_url and records[2].enriched_url
    assert records[1].enriched_url is None  # the 500 — failed, others unaffected
    assert records[3].enrichment_attempted is False


def test_enrichment_failure_is_swallowed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _mock_httpx(monkeypatch, handler)
    record = RasffRecord.model_validate(ALERT)
    # A wholesale SPA outage yields zero enrichments but never raises; the record stays valid.
    assert enrich_records([record]) == 0
    assert record.enriched_url is None
    row = normalize_rasff(record)
    assert row["source_url"].startswith("https://webgate.ec.europa.eu/rasff-window/")


def test_fetch_paginates_filters_and_enriches(monkeypatch):
    page1 = {
        "value": [ALERT, BORDER],  # BORDER must be filtered out (not a recall)
        "nextLink": "https://api.datalake.sante.service.ec.europa.eu/rasff/next-page",
    }
    page2 = {"value": [{**ALERT, "NOTIFICATION_REFERENCE": "2026.5975"}], "nextLink": None}
    pages = iter([page1, page2])
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=next(pages))

    _mock_httpx(monkeypatch, handler)
    records = fetch_rasff(days=7, enrich=False)
    refs = [r.reference for r in records]
    assert refs == ["2026.5974", "2026.5975"]  # both alerts kept, border dropped, both pages read
    # The datalake API 400s on a percent-encoded timestamp, so the colons must go over the wire
    # raw (NOTIF_DATE_FROM=…T00:00:00Z, never …T00%3A00%3A00Z). Guards the httpx-params regression
    # that broke the daily ingest.
    first = seen_urls[0]
    assert "NOTIF_DATE_FROM=" in first and first.endswith("T00:00:00Z")
    assert "%3A" not in first
    assert "NETWORK_DESC=RASFF" in first


def test_stats_payload_cached_before_by_affected_country_still_validates():
    # RecallStats gained by_affected_country for the EU map. rebuild_stats stores payloads as
    # snake_case model_dump(mode="json"); rows materialized before the field existed must keep
    # validating, since get_stats serves the cached payload until the next rebuild replaces it.
    legacy_payload = {
        "total": 0,
        "by_category": [],
        "by_month": [],
        "by_classification": [],
        "by_severity": [],
        "by_state": [],
        "by_company": [],
        "by_source": [],
        "by_entity": [],
        "anomalies": [],
        "forecast": [],
        "last_ingest_at": None,
    }
    assert RecallStats.model_validate(legacy_payload).by_affected_country == []


def test_enrichment_backfill_filters_the_source_the_normalizer_writes():
    # Guard the bug that made the enrichment backfill match zero rows: it filtered Recall.source on
    # the ingest-job id ("rasff_eu") instead of the RecallSource enum value the normalizer actually
    # writes ("rasff"). Compile the work-list query and assert it targets the written value and the
    # fallback host — DB-free, so it runs in the default suite.
    written_source = normalize_rasff(RasffRecord.model_validate(ALERT))["source"]
    sql = str(service._rasff_enrichment_stmt().compile(compile_kwargs={"literal_binds": True}))
    assert f"'{written_source}'" in sql  # 'rasff' — the value stored on the row
    assert "'rasff_eu'" not in sql  # never the ingest-job id
    assert RASFF_WINDOW_HOST in sql  # targets rows still on the RASFF Window fallback URL
