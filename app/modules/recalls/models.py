from datetime import date, datetime

from sqlalchemy import Computed, Date, DateTime, Float, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.modules.recalls.entities import Entity

# Searchable text for full-text search — kept identical to the migration's generated column.
_SEARCH_EXPR = (
    "to_tsvector('english', "
    "coalesce(product_description, '') || ' ' || "
    "coalesce(reason_text, '') || ' ' || "
    "coalesce(company_name, ''))"
)


class Recall(Base):
    __tablename__ = "recalls"

    source: Mapped[str] = mapped_column(Text, primary_key=True, server_default="fda")
    country: Mapped[str] = mapped_column(Text, server_default="us", index=True)
    recall_number: Mapped[str] = mapped_column(Text, primary_key=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    event_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str | None] = mapped_column(Text)
    product_description: Mapped[str] = mapped_column(Text)
    reason_text: Mapped[str] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    # Affected-state codes for the map / state filter. FDA = [recalling-firm state]; FSIS = the
    # (often multiple) distribution states. `state` keeps the single display value.
    # none_as_null so a Python None is stored as SQL NULL, not a JSON 'null' scalar (which would
    # break jsonb_array_elements_text in the by-state aggregation).
    states: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    distribution_pattern: Mapped[str | None] = mapped_column(Text)
    recall_initiation_date: Mapped[date | None] = mapped_column(Date)
    # Indexed: report_date backs the default ordering + `since` filter + monthly stats; category
    # backs the category filter + per-category stats. See migration ...add_recall_indexes.
    report_date: Mapped[date | None] = mapped_column(Date, index=True)
    category: Mapped[str] = mapped_column(Text, index=True)
    category_confidence: Mapped[float] = mapped_column(Float)
    # Composite 0–100 severity (severity.py) + its band. Indexed: severity_score backs the
    # `sort=severity` ordering and the `minSeverity` filter. New rows get both at ingest; the
    # server defaults only seed pre-existing rows until scripts/backfill_severity.py runs.
    severity_score: Mapped[float] = mapped_column(Float, index=True, server_default=text("0"))
    severity_label: Mapped[str] = mapped_column(Text, server_default=text("'low'"))
    # NMF topic assigned by scripts/build_analytics.py (recall_topics.id). Indexed for the `topic`
    # filter. NULL until the analytics build runs (and for rows with no usable text).
    topic_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # Allergens / pathogens / hazards / contaminants extracted from reason_text (gazetteer match)
    # as [{type, value}]. GIN-indexed for the `@>` entity filter (the by-entity aggregation unnests,
    # so it can't use the index).
    entities: Mapped[list[Entity]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Full-text search over product/reason/company — generated tsvector, GIN-indexed (deferred so
    # the list query doesn't load it). Expression matches migration ...add_recall_search.
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_SEARCH_EXPR, persisted=True), deferred=True
    )
    raw: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    upserted_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(Text)
    error_text: Mapped[str | None] = mapped_column(Text)


# Derived analytics, materialized offline by scripts/build_analytics.py from one shared TF-IDF
# matrix. Served as cheap indexed reads — the model is never loaded at request time.
class RecallTopic(Base):
    __tablename__ = "recall_topics"

    # id is the NMF component index (assigned, not autoincremented) — matches recalls.topic_id.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    label: Mapped[str] = mapped_column(Text)
    top_terms: Mapped[list[str]] = mapped_column(JSONB)
    size: Mapped[int] = mapped_column(Integer)


class RecallNeighbor(Base):
    __tablename__ = "recall_neighbors"

    # The recall whose neighbors these are (FK-ish to recalls' composite PK), its rank-ordered
    # nearest neighbors by cosine similarity over the TF-IDF matrix. Top-K rows per recall.
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    recall_number: Mapped[str] = mapped_column(Text, primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    neighbor_source: Mapped[str] = mapped_column(Text)
    neighbor_number: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
