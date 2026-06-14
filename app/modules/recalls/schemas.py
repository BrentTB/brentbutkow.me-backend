from datetime import date, datetime
from enum import StrEnum

from pydantic import Field

from app.camel import CamelModel


# Derived enums — values are the runtime identifiers shared with the frontend.
class RecallCategory(StrEnum):
    allergen = "allergen"
    pathogen = "pathogen"
    foreign_material = "foreignMaterial"
    mislabeling = "mislabeling"
    other = "other"


class RecallClass(StrEnum):
    class_i = "Class I"
    class_ii = "Class II"
    class_iii = "Class III"
    public_health_alert = "Public Health Alert"


class RecallSource(StrEnum):
    fda = "fda"
    usda = "usda"


class RecallOut(CamelModel):
    source: RecallSource = Field(description="Data source: fda (openFDA) or usda (FSIS).")
    recall_number: str = Field(
        description="Recall number, unique per source.", examples=["007-2026"]
    )
    source_url: str | None = Field(
        description="Canonical recall page — FSIS provides one; FDA does not."
    )
    status: str | None = Field(description="Status (FDA: Ongoing/Terminated; FSIS: Active/Closed).")
    classification: RecallClass | None = Field(
        description="Recall class (severity), or Public Health Alert."
    )
    product_description: str = Field(description="The recalled product.")
    reason_text: str = Field(description="Why it was recalled.")
    company_name: str | None = Field(description="Recalling firm / establishment.")
    state: str | None = Field(description="Single recalling-firm / primary state.")
    states: list[str] | None = Field(description="All affected-state codes (used by the map).")
    distribution_pattern: str | None = Field(description="Where the product was distributed.")
    recall_initiation_date: date | None = Field(description="When the recall began.")
    report_date: date | None = Field(description="When it was reported.")
    category: RecallCategory = Field(description="Predicted cause category from the classifier.")
    category_confidence: float = Field(description="Classifier confidence in [0, 1].")


class RecallListResult(CamelModel):
    items: list[RecallOut]
    total: int


class CategoryCount(CamelModel):
    category: str
    count: int


class MonthCount(CamelModel):
    month: str
    count: int


class LabelCount(CamelModel):
    label: str
    count: int


class RecallStats(CamelModel):
    total: int
    by_category: list[CategoryCount]
    by_month: list[MonthCount]
    by_classification: list[LabelCount]
    by_state: list[LabelCount]
    by_company: list[LabelCount]
    by_source: list[LabelCount]
    last_ingest_at: datetime | None


class IngestResult(CamelModel):
    status: str
    fetched: int
    upserted: int
