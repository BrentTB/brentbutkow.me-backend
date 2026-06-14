from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
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
    recall_number: str
    status: str | None
    classification: str | None
    product_description: str
    reason_text: str
    company_name: str | None
    state: str | None
    distribution_pattern: str | None
    recall_initiation_date: date | None
    report_date: date | None
    category: str
    category_confidence: float


class RecallListResult(CamelModel):
    items: list[RecallOut]
    total: int


class CategoryCount(CamelModel):
    category: str
    count: int


class MonthCount(CamelModel):
    month: str
    count: int


class RecallStats(CamelModel):
    total: int
    by_category: list[CategoryCount]
    by_month: list[MonthCount]
    last_ingest_at: datetime | None


class IngestResult(CamelModel):
    status: str
    fetched: int
    upserted: int
