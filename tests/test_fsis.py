from app.modules.recalls import fsis
from app.modules.recalls.fsis import FsisRecord, normalize_fsis
from app.modules.recalls.schemas import RecallCategory

# Trimmed from real FSIS API responses (a recall and a public-health-alert row).
RECALL = {
    "field_title": "Synear Foods USA, LLC Recalls Frozen Pork and Crab Soup Dumpling Products",
    "field_recall_number": "007-2026",
    "field_recall_url": "http://www.fsis.usda.gov/recalls-alerts/synear-foods-usa-llc-recalls",
    "field_active_notice": "True",
    "field_states": ["California", "New Jersey", "Washington"],
    "field_establishment": ["Synear Foods USA, LLC"],
    "field_product_items": ['13.23-oz. bags of "Synear SUPREME SOUP DUMPLING PORK &amp; CRAB".'],
    "field_recall_classification": "Class I",
    "field_recall_date": "2026-05-31",
    "field_recall_reason": ["Misbranding", "Unreported Allergens"],
    "field_summary": "<p><strong>Note:</strong> revised BEST BY date.</p>",
    "langcode": "English",
}

PHA = {
    "field_title": "FSIS Issues Public Health Alert for Ineligible Meat and Poultry Products",
    "field_recall_number": "PHA-10242024-01",
    "field_active_notice": "False",
    "field_states": [
        "Arizona",
        "California",
        "Iowa",
        "Kansas",
        "Maryland",
        "Minnesota",
        "Nebraska",
        "Oklahoma",
        "Texas",
    ],
    "field_establishment": [],
    "field_product_items": ['180-g. cans containing "BEST BEEF CURRY."'],
    "field_recall_classification": "Public Health Alert",
    "field_recall_date": "2024-10-24",
    "field_recall_reason": ["Import Violation"],
    "field_summary": "<p>WASHINGTON, Oct. 24, 2024 - FSIS issued an alert.</p>",
    "langcode": "English",
}


def _normalize(monkeypatch, raw):
    # Isolate normalization from the ML model — assert mapping, not the classifier's output.
    monkeypatch.setattr(fsis, "classify", lambda text: (RecallCategory.allergen, 0.9))
    return normalize_fsis(FsisRecord.model_validate(raw))


def test_normalize_recall(monkeypatch):
    row = _normalize(monkeypatch, RECALL)
    assert row["source"] == "usda"
    assert row["recall_number"] == "007-2026"
    assert row["source_url"].startswith("http")
    assert row["classification"] == "Class I"
    assert row["company_name"] == "Synear Foods USA, LLC"
    assert row["status"] == "Active"
    assert row["states"] == ["CA", "NJ", "WA"]  # full names mapped to codes
    assert row["state"] is None  # multi-state → no single display value
    assert row["report_date"].isoformat() == "2026-05-31"
    assert "Misbranding" in row["reason_text"]
    assert "&amp;" not in row["product_description"]  # HTML entities decoded
    assert "&" in row["product_description"]
    assert row["category"] == "allergen"
    # Entities come from reason_text only — "Misbranding, Unreported Allergens" names no specific
    # allergen, and "CRAB" in the product description is (correctly) not matched.
    assert row["entities"] == []


def test_normalize_public_health_alert(monkeypatch):
    row = _normalize(monkeypatch, PHA)
    assert row["classification"] == "Public Health Alert"  # first-class, searchable
    assert row["company_name"] is None  # empty establishment array
    assert row["status"] == "Closed"
    assert len(row["states"]) == 9
    assert row["recall_number"] == "PHA-10242024-01"


def test_map_states_drops_unknown_names():
    assert fsis._map_states(["California", "Atlantis"]) == ["CA"]
    assert fsis._map_states([]) is None


def test_strip_html_unescapes_and_removes_tags():
    assert fsis._strip_html("<p>Undeclared milk &amp; soy</p>") == "Undeclared milk & soy"
