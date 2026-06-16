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
    # US — FDA severity classes + FSIS public-health alerts.
    class_i = "Class I"
    class_ii = "Class II"
    class_iii = "Class III"
    public_health_alert = "Public Health Alert"
    # UK — FSA alert types.
    product_recall = "Product Recall"
    allergy_alert = "Allergy Alert"
    food_alert_for_action = "Food Alert for Action"


class RecallSource(StrEnum):
    fda = "fda"
    usda = "usda"
    uk = "uk"


class RecallCountry(StrEnum):
    us = "us"
    uk = "uk"


class EntityType(StrEnum):
    allergen = "allergen"
    pathogen = "pathogen"
    hazard = "hazard"


class RecallEntity(CamelModel):
    type: EntityType = Field(description="Entity kind: allergen, pathogen, or hazard.")
    value: str = Field(description="Canonical entity name.", examples=["peanuts", "Listeria"])


class RecallOut(CamelModel):
    country: RecallCountry = Field(description="Country the recall is from: us or uk.")
    source: RecallSource = Field(description="Data source: fda (openFDA), usda (FSIS), uk (FSA).")
    recall_number: str = Field(
        description="Recall number, unique per source.", examples=["007-2026"]
    )
    source_url: str | None = Field(
        description="Canonical recall page — FSIS provides one; FDA does not."
    )
    status: str | None = Field(description="Status (FDA: Ongoing/Terminated; FSIS: Active/Closed).")
    classification: RecallClass | None = Field(
        description="Recall class / alert type (US: Class I-III, PHA; UK: PRIN/AA/FAFA)."
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
    entities: list[RecallEntity] = Field(
        default_factory=list, description="Allergens, pathogens, and hazards found in the reason."
    )


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


class EntityCount(CamelModel):
    type: EntityType
    label: str
    count: int


class AnomalyScope(StrEnum):
    overall = "overall"
    category = "category"
    entity = "entity"


class AnomalyMonth(CamelModel):
    month: str = Field(description="The anomalous month (YYYY-MM).", examples=["2026-03"])
    observed: int = Field(description="Recall count that month.")
    baseline: float = Field(description="Median count of the trailing baseline window.")
    z: float = Field(description="Robust z-score; sign gives direction (+ spike, - dip).")


class Anomaly(CamelModel):
    scope: AnomalyScope = Field(
        description="What is anomalous: overall volume, a category, or an entity."
    )
    label: str = Field(description="Human label for the scope, e.g. 'All recalls' or 'Listeria'.")
    months: list[AnomalyMonth] = Field(
        description="Every flagged month for this thing in the window (consolidated, ≥1)."
    )
    series: list[MonthCount] = Field(
        default_factory=list,
        description="Monthly counts over the displayed window, for charting the anomaly.",
    )


class RecallStats(CamelModel):
    total: int
    by_category: list[CategoryCount]
    by_month: list[MonthCount]
    by_classification: list[LabelCount]
    by_state: list[LabelCount]
    by_company: list[LabelCount]
    by_source: list[LabelCount]
    by_entity: list[EntityCount]
    anomalies: list[Anomaly]
    last_ingest_at: datetime | None


class IngestResult(CamelModel):
    status: str
    fetched: int
    upserted: int
