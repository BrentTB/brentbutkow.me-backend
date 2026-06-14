from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


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


# snake_case fields in Python, camelCase JSON on the wire (FastAPI serializes by alias).
class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class RecallOut(CamelModel):
    recall_number: str = Field(
        description="openFDA recall number (unique).", examples=["F-1421-2026"]
    )
    status: str | None = Field(description="openFDA status, e.g. Ongoing or Terminated.")
    classification: RecallClass | None = Field(description="FDA recall class (severity).")
    product_description: str = Field(description="The recalled product.")
    reason_text: str = Field(description="Why it was recalled (openFDA reason text).")
    company_name: str | None = Field(description="Recalling firm.")
    state: str | None = Field(description="Recalling firm's state.")
    distribution_pattern: str | None = Field(description="Where the product was distributed.")
    recall_initiation_date: date | None = Field(description="When the recall began.")
    report_date: date | None = Field(description="When openFDA reported it.")
    category: RecallCategory = Field(
        description="Predicted cause category from the recall classifier."
    )
    category_confidence: float = Field(
        description=(
            "Classifier confidence in [0, 1]: the model's predicted probability for the chosen "
            "category, or 1.0/0.0 from the keyword fallback when no model is loaded."
        )
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


class RecallStats(CamelModel):
    total: int
    by_category: list[CategoryCount]
    by_month: list[MonthCount]
    by_classification: list[LabelCount]
    by_state: list[LabelCount]
    by_company: list[LabelCount]
    last_ingest_at: datetime | None


class IngestResult(CamelModel):
    status: str
    fetched: int
    upserted: int
