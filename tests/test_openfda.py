from datetime import date

import pytest
from pydantic import ValidationError

from app.modules.recalls import openfda
from app.modules.recalls.openfda import OpenFdaRecord, OpenFdaResponse, normalize_recall
from app.modules.recalls.schemas import RecallCategory


def test_normalize_maps_openfda_fields_to_domain():
    record = OpenFdaRecord(
        recall_number="F-0276-2017",
        classification="Class II",
        product_description="CytoDetox",
        reason_for_recall="Product contains undeclared milk.",
        recalling_firm="Pharmatech LLC",
        state="FL",
        recall_initiation_date="20160808",
        report_date="20161102",
    )
    result = normalize_recall(record)
    assert result["recall_number"] == "F-0276-2017"
    assert result["company_name"] == "Pharmatech LLC"
    assert result["classification"] == "Class II"
    assert result["recall_initiation_date"] == date(2016, 8, 8)
    assert result["report_date"] == date(2016, 11, 2)
    assert result["category"] == RecallCategory.allergen.value


def test_normalize_handles_missing_and_invalid_values():
    result = normalize_recall(OpenFdaRecord(recall_number="X-1", classification="Bogus"))
    assert result["company_name"] is None
    assert result["classification"] is None
    assert result["report_date"] is None
    assert result["product_description"] == ""
    assert result["category"] == RecallCategory.other.value


def test_response_validation():
    valid = OpenFdaResponse.model_validate({"results": [{"recall_number": "A-1"}]})
    assert len(valid.results) == 1
    with pytest.raises(ValidationError):
        OpenFdaResponse.model_validate({"results": [{"event_id": "1"}]})


def test_fetch_enforcement_paginates_and_stops_on_short_page(monkeypatch):
    pages = {
        0: [OpenFdaRecord(recall_number=f"R-{i}") for i in range(1000)],
        1000: [OpenFdaRecord(recall_number=f"R-{i}") for i in range(1000, 2000)],
        2000: [OpenFdaRecord(recall_number=f"R-{i}") for i in range(2000, 2400)],  # short page
    }

    def fake_fetch_page(skip: int, limit: int) -> list[OpenFdaRecord]:
        return pages.get(skip, [])[:limit]

    monkeypatch.setattr(openfda, "_fetch_page", fake_fetch_page)

    result = openfda.fetch_enforcement(limit=5000)
    assert len(result) == 2400  # walked three pages, stopped at the short one (no infinite loop)
