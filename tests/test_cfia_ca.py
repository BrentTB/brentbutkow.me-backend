from app.modules.recalls import cfia_ca
from app.modules.recalls.cfia_ca import CfiaRecord, _classification, fetch_cfia, normalize_cfia
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.schemas import RecallCategory
from app.modules.recalls.severity import score_severity

# Trimmed from a real Health Canada open-data record (Organization == "CFIA").
RECALL = {
    "NID": "98765",
    "Title": "Recalled brand cheese may contain Listeria monocytogenes",
    "URL": "https://recalls-rappels.canada.ca/en/alert-recall/recalled-brand-cheese",
    "Organization": "CFIA",
    "Product": "ACME Soft Cheese 200g",
    "Issue": "Microbial contamination - Listeria monocytogenes",
    "Recall class": "Class 1",
    "Last updated": "2026-03-14",
    "Archived": "0",
}

# A non-food row from the same file — the consumer-product organization we filter out.
NON_FOOD = {
    "NID": "82252",
    "Title": "Certain food jars recalled due to laceration hazard",
    "Organization": "Consumer product safety",
    "Product": "Stainless King Food Jar",
    "Issue": "Laceration hazard",
    "Recall class": "",
    "Last updated": "2026-06-29",
    "Archived": "1",
}


def _normalize(monkeypatch, raw):
    monkeypatch.setattr(cfia_ca, "classify", lambda text: (RecallCategory.pathogen, 0.8))
    return normalize_cfia(CfiaRecord.model_validate(raw))


def test_normalize_recall(monkeypatch):
    row = _normalize(monkeypatch, RECALL)
    assert row["source"] == "cfia"
    assert row["country"] == "ca"
    assert row["recall_number"] == "98765"
    assert row["source_url"].startswith("https://recalls-rappels.canada.ca")
    assert row["classification"] == "Class I"  # CFIA Class 1 → FDA-scale Class I
    assert row["status"] == "Active"  # Archived == "0"
    assert row["company_name"] is None  # not in the feed
    assert row["state"] is None and row["states"] is None  # no Canadian geography
    assert row["report_date"].isoformat() == "2026-03-14"
    assert row["recall_initiation_date"].isoformat() == "2026-03-14"
    assert "listeria" in row["reason_text"].lower()
    assert "ACME" in row["product_description"]
    assert row["category"] == "pathogen"
    assert {"type": "pathogen", "value": "Listeria"} in row["entities"]
    # No firm name or geography, so severity rests on class + category + entities, like the UK path.
    expected_score, expected_label = score_severity(
        classification="Class I",
        category=RecallCategory.pathogen.value,
        entities=extract_entities(RECALL["Issue"]),
        reason_text=RECALL["Issue"],
    )
    assert row["severity_score"] == expected_score
    assert row["severity_label"] == expected_label


def test_archived_status(monkeypatch):
    row = _normalize(monkeypatch, {**RECALL, "Archived": "1"})
    assert row["status"] == "Archived"


def test_classification_maps_canadian_classes():
    assert _classification("Class 1") == "Class I"
    assert _classification("Class 2") == "Class II"
    assert _classification("Class 3") == "Class III"
    assert _classification("Class 1 - Class 2") == "Class I"  # most severe wins
    assert _classification("--") is None
    assert _classification("") is None
    assert _classification(None) is None


def test_fetch_keeps_only_cfia_food(monkeypatch):
    def fake_get(url, **kwargs):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return [RECALL, NON_FOOD]

        return _Resp()

    monkeypatch.setattr(cfia_ca.httpx, "get", fake_get)
    records = fetch_cfia()
    assert [r.nid for r in records] == ["98765"]  # the consumer-product row is dropped
