from app.modules.recalls import fsa_uk
from app.modules.recalls.entities import extract_entities
from app.modules.recalls.fsa_uk import FsaRecord, normalize_fsa
from app.modules.recalls.schemas import RecallCategory
from app.modules.recalls.severity import score_severity

# Trimmed from a real FSA Food Alerts API record.
ALERT = {
    "notation": "FSA-PRIN-01-2018",
    "title": "James Hall recalls BBQ Pulled Pork because it may contain Salmonella",
    "created": "2018-01-18",
    "type": [
        "http://data.food.gov.uk/food-alerts/def/Alert",
        "http://data.food.gov.uk/food-alerts/def/PRIN",
    ],
    "status": {"label": "Published"},
    "alertURL": "https://www.food.gov.uk/news-alerts/alert/FSA-PRIN-01-2018",
    "reportingBusiness": {"commonName": "James Hall"},
    "problem": [{"riskStatement": "The products might be contaminated with salmonella."}],
    "productDetails": [{"productName": "SPAR BBQ Pulled Pork"}, {"productName": "Woodland BBQ"}],
}


def _normalize(monkeypatch, raw):
    monkeypatch.setattr(fsa_uk, "classify", lambda text: (RecallCategory.pathogen, 0.8))
    return normalize_fsa(FsaRecord.model_validate(raw))


def test_normalize_alert(monkeypatch):
    row = _normalize(monkeypatch, ALERT)
    assert row["source"] == "uk"
    assert row["country"] == "uk"
    assert row["recall_number"] == "FSA-PRIN-01-2018"
    assert row["source_url"].startswith("https://www.food.gov.uk")
    assert row["classification"] == "Product Recall"  # PRIN
    assert row["company_name"] == "James Hall"
    assert row["status"] == "Published"
    assert row["state"] is None and row["states"] is None  # no US geography
    assert row["report_date"].isoformat() == "2018-01-18"
    assert "salmonella" in row["reason_text"].lower()
    assert "BBQ" in row["product_description"]
    assert row["category"] == "pathogen"
    assert {"type": "pathogen", "value": "Salmonella"} in row["entities"]
    # UK alerts carry no US geography, so severity rests on classification + category + entities
    # (Salmonella is a deadliest-entity bonus). Re-derive from those inputs only.
    expected_score, expected_label = score_severity(
        classification="Product Recall",
        category=RecallCategory.pathogen.value,
        entities=extract_entities("The products might be contaminated with salmonella."),
        reason_text="The products might be contaminated with salmonella.",
    )
    assert row["severity_score"] == expected_score
    assert row["severity_label"] == expected_label


def test_classification_maps_alert_types():
    base = "http://data.food.gov.uk/food-alerts/def/"
    assert fsa_uk._classification([base + "Alert", base + "AA"]) == "Allergy Alert"
    assert fsa_uk._classification([base + "FAFA"]) == "Food Alert for Action"
    assert fsa_uk._classification([base + "Alert"]) is None
