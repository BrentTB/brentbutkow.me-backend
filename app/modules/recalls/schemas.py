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
    contaminant = "contaminant"
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
    ncc = "ncc"  # South Africa — National Consumer Commission
    woolworths = "woolworths"  # South Africa — Woolworths Holdings (curated seed)
    shoprite = "shoprite"  # South Africa — Shoprite / Checkers (curated seed)
    nrcs = "nrcs"  # South Africa — National Regulator for Compulsory Specifications (curated seed)


class RecallCountry(StrEnum):
    us = "us"
    uk = "uk"
    za = "za"


class EntityType(StrEnum):
    allergen = "allergen"
    pathogen = "pathogen"
    hazard = "hazard"
    contaminant = "contaminant"


class SeverityLabel(StrEnum):
    # Bands over the 0–100 severity_score — see app/modules/recalls/severity.py for the thresholds.
    low = "low"
    moderate = "moderate"
    high = "high"
    severe = "severe"


class RecallSort(StrEnum):
    recency = "recency"  # most recent report_date first (the default)
    severity = "severity"  # highest severity_score first, then most recent


class RecallEntity(CamelModel):
    type: EntityType = Field(description="Entity kind: allergen, pathogen, hazard, or contaminant.")
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
    severity_score: float = Field(
        description=(
            "Composite 0–100 severity, blending the recall's classification, cause category, the "
            "deadliest named entities, allergen risk tier, reported harm, and geographic breadth "
            "onto one US/UK-comparable scale."
        )
    )
    severity_label: SeverityLabel = Field(
        description="Banded severity: low, moderate, high, or severe."
    )
    topic_id: int | None = Field(
        default=None,
        description="NMF theme id (recall_topics.id); null until the analytics build runs.",
    )
    event_cluster_id: int | None = Field(
        default=None,
        description="Event/outbreak cluster id (recall_events.id); null until events are built.",
    )
    entities: list[RecallEntity] = Field(
        default_factory=list,
        description="Allergens, pathogens, hazards, and contaminants found in the reason.",
    )


class RecallListResult(CamelModel):
    items: list[RecallOut]
    total: int


class TopicOut(CamelModel):
    id: int = Field(description="Surrogate topic id; also stored on each recall as topicId.")
    slug: str = Field(
        description="Stable key from the terms; use as the `topic` filter (survives a rebuild).",
        examples=["listeria-deli-meat"],
    )
    label: str = Field(
        description="Human label — the topic's top terms.", examples=["listeria · deli · meat"]
    )
    top_terms: list[str] = Field(description="Ranked terms describing the topic.")
    size: int = Field(description="Number of recalls assigned to this topic.")


class EventOut(CamelModel):
    id: int = Field(description="Surrogate cluster id; also on each recall as eventClusterId.")
    slug: str = Field(
        description="Stable key (entity + first month); use as the `event` filter.",
        examples=["listeria-2026-03"],
    )
    label: str = Field(description="Human label.", examples=["Listeria · 7 recalls"])
    is_outbreak: bool = Field(
        description="True for the high-signal subset: multi-recall and pathogen-driven."
    )
    dominant_entity: str | None = Field(
        default=None, description="The cluster's main hazard (pathogen/allergen), if any."
    )
    recall_count: int = Field(description="Recalls in the cluster.")
    company_count: int = Field(description="Distinct companies involved.")
    state_count: int = Field(description="Distinct US states affected (0 for UK).")
    first_date: date | None = Field(default=None, description="Earliest member report date.")
    last_date: date | None = Field(default=None, description="Latest member report date.")
    severity_max: float = Field(description="Highest member severity score (0–100).")


class SimilarRecall(CamelModel):
    similarity: float = Field(description="Cosine similarity to the queried recall, in [0, 1].")
    recall: RecallOut = Field(description="The similar recall.")


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
    z: float = Field(
        description=(
            "Robust z-score vs the trailing baseline; positive (dips never flag), and may sit "
            "below the spike threshold when the month flagged as a near-record high."
        )
    )


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


class ForecastPoint(CamelModel):
    month: str = Field(description="Projected month (YYYY-MM).", examples=["2026-07"])
    predicted: float = Field(description="Projected recall count for the month (≥ 0).")
    lower: float = Field(description="Lower edge of the ~1σ typical-error band (≥ 0).")
    upper: float = Field(description="Upper edge of the ~1σ typical-error band.")


class TrendGroup(StrEnum):
    total = "total"
    category = "category"
    source = "source"
    severity = "severity"
    classification = "classification"


class TrendBucket(CamelModel):
    month: str = Field(description="Month bucket (YYYY-MM).")
    group: str = Field(description="Group key — a category/source value, or 'total'.")
    count: int


class TrendResult(CamelModel):
    group: TrendGroup = Field(description="The dimension the monthly counts are grouped by.")
    buckets: list[TrendBucket] = Field(description="Long-format (month, group, count) rows.")


class RecallStats(CamelModel):
    total: int
    by_category: list[CategoryCount]
    by_month: list[MonthCount]
    by_classification: list[LabelCount]
    by_severity: list[LabelCount]
    by_state: list[LabelCount]
    by_company: list[LabelCount]
    by_source: list[LabelCount]
    by_entity: list[EntityCount]
    anomalies: list[Anomaly]
    forecast: list[ForecastPoint] = Field(
        default_factory=list,
        description=(
            "Short-horizon projection of overall monthly volume with a band; empty when history is "
            "too short to forecast. A projection, not a record of what happened (see anomalies)."
        ),
    )
    last_ingest_at: datetime | None


class IngestResult(CamelModel):
    status: str
    fetched: int
    # Rows that were genuinely new (not already stored), vs. `upserted` which counts every row
    # written — new and re-seen alike, so it's almost always just the fetched total.
    new: int
    upserted: int
