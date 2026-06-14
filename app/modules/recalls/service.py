from collections.abc import Iterable
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.recalls.fsa_uk import fetch_fsa, normalize_fsa
from app.modules.recalls.fsis import fetch_fsis, normalize_fsis
from app.modules.recalls.models import IngestRun, Recall
from app.modules.recalls.openfda import fetch_enforcement, normalize_recall
from app.modules.recalls.schemas import (
    CategoryCount,
    IngestResult,
    LabelCount,
    MonthCount,
    RecallListResult,
    RecallOut,
    RecallStats,
)

# Rows per upsert statement — keeps a large backfill to a few statements instead of thousands.
_UPSERT_CHUNK = 500

# How many rows to return for the high-cardinality company breakdown.
_TOP_N = 15

# Recalls are identified by (source, recall_number) — the dedupe + upsert conflict key.
_CONFLICT_KEYS = ("source", "recall_number")

# Which ingest sources belong to each country — scopes the "last updated" timestamp.
_COUNTRY_SOURCES = {"us": ("openfda_food", "usda_fsis"), "uk": ("uk_fsa",)}


def _dedupe(rows: Iterable[dict]) -> list[dict]:
    # Keep the last row per identity so a multi-row upsert can't touch the same PK twice.
    return list({(r["source"], r["recall_number"]): r for r in rows}.values())


def _upsert_recalls(session: Session, rows: list[dict]) -> None:
    for start in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[start : start + _UPSERT_CHUNK]
        insert_stmt = pg_insert(Recall).values(chunk)
        update_set = {
            key: insert_stmt.excluded[key] for key in chunk[0] if key not in _CONFLICT_KEYS
        }
        update_set["updated_at"] = datetime.now(UTC)
        session.execute(
            insert_stmt.on_conflict_do_update(index_elements=list(_CONFLICT_KEYS), set_=update_set)
        )


def _redact_secrets(message: str) -> str:
    # httpx exception strings embed the request URL, which carries ?api_key=<secret>.
    # Strip it before the message is persisted to ingest_runs.error_text.
    secret = settings.openfda_api_key
    return message.replace(secret, "***") if secret else message


def list_recalls(
    session: Session,
    *,
    limit: int,
    offset: int,
    country: str | None = None,
    source: str | None = None,
    category: str | None = None,
    classification: str | None = None,
    state: str | None = None,
    company: str | None = None,
    since: date | None = None,
    search: str | None = None,
) -> RecallListResult:
    # Treat blank/whitespace-only as "no search" so it doesn't build a no-op tsquery + ranking.
    search = search.strip() if search else None

    conditions = []
    if country:
        conditions.append(Recall.country == country)
    if source:
        conditions.append(Recall.source == source)
    if category:
        conditions.append(Recall.category == category)
    if classification:
        conditions.append(Recall.classification == classification)
    if state:
        # `states` is the array of affected states; match if it contains the requested code.
        conditions.append(Recall.states.contains([state]))
    if company:
        # Leading-wildcard ILIKE can't use a btree index, so this is a seq scan. The table grows
        # slowly — a few new openFDA recalls a day on top of the initial ~26k-row backfill — so it
        # stays cheap for a long time. Revisit with a pg_trgm GIN index if it ever gets large.
        conditions.append(Recall.company_name.ilike(f"%{company}%"))
    if since:
        conditions.append(Recall.report_date >= since)
    if search:
        conditions.append(
            Recall.search_vector.op("@@")(func.websearch_to_tsquery("english", search))
        )

    stmt = select(Recall)
    count_stmt = select(func.count()).select_from(Recall)
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    # Rank by text relevance when searching, then fall back to most-recent-first.
    ordering = []
    if search:
        ordering.append(
            func.ts_rank(Recall.search_vector, func.websearch_to_tsquery("english", search)).desc()
        )
    ordering.append(Recall.report_date.desc().nulls_last())
    rows = session.scalars(stmt.order_by(*ordering).limit(limit).offset(offset)).all()
    total = session.scalar(count_stmt) or 0

    return RecallListResult(items=[RecallOut.model_validate(row) for row in rows], total=total)


def get_stats(session: Session, country: str | None = None) -> RecallStats:
    def scoped(stmt):
        # US and UK are shown separately, so every aggregation is scoped to the chosen country.
        return stmt.where(Recall.country == country) if country else stmt

    by_category = session.execute(
        scoped(select(Recall.category, func.count()).group_by(Recall.category))
    ).all()

    month = func.to_char(Recall.report_date, "YYYY-MM")
    by_month = session.execute(
        scoped(
            select(month.label("month"), func.count())
            .where(Recall.report_date.is_not(None))
            .group_by(month)
            .order_by(month)
        )
    ).all()

    by_classification = session.execute(
        scoped(
            select(Recall.classification, func.count())
            .where(Recall.classification.is_not(None))
            .group_by(Recall.classification)
            .order_by(Recall.classification)
        )
    ).all()
    # Count each affected state by unnesting the `states` array, so a multi-state FSIS recall
    # counts toward every state it touches. Bounded set (~50) — the leaderboard slices it.
    states_elem = func.jsonb_array_elements_text(Recall.states).table_valued(
        "value", joins_implicitly=True
    )
    by_state = session.execute(
        scoped(
            select(states_elem.c.value, func.count())
            .select_from(Recall, states_elem)
            .where(func.jsonb_typeof(Recall.states) == "array")
            .group_by(states_elem.c.value)
            .order_by(func.count().desc())
        )
    ).all()
    by_company = session.execute(
        scoped(
            select(Recall.company_name, func.count())
            .where(Recall.company_name.is_not(None))
            .group_by(Recall.company_name)
            .order_by(func.count().desc())
            .limit(_TOP_N)
        )
    ).all()
    by_source = session.execute(
        scoped(
            select(Recall.source, func.count())
            .group_by(Recall.source)
            .order_by(func.count().desc())
        )
    ).all()

    total = session.scalar(scoped(select(func.count()).select_from(Recall))) or 0

    ingest_stmt = select(IngestRun.finished_at).where(IngestRun.status == "ok")
    if country:
        ingest_stmt = ingest_stmt.where(IngestRun.source.in_(_COUNTRY_SOURCES.get(country, ())))
    last_ingest_at = session.scalar(ingest_stmt.order_by(IngestRun.finished_at.desc()).limit(1))

    return RecallStats(
        total=total,
        by_category=[
            CategoryCount(category=category, count=count) for category, count in by_category
        ],
        by_month=[MonthCount(month=month_label, count=count) for month_label, count in by_month],
        by_classification=[
            LabelCount(label=label, count=count) for label, count in by_classification
        ],
        by_state=[LabelCount(label=label, count=count) for label, count in by_state],
        by_company=[LabelCount(label=label, count=count) for label, count in by_company],
        by_source=[LabelCount(label=label, count=count) for label, count in by_source],
        last_ingest_at=last_ingest_at,
    )


def run_fsis_ingest(session: Session) -> IngestResult:
    run = IngestRun(source="usda_fsis", status="running")
    session.add(run)
    session.commit()
    try:
        records = fetch_fsis()
        rows = _dedupe(normalize_fsis(record) for record in records)
        _upsert_recalls(session, rows)
        run.finished_at = datetime.now(UTC)
        run.fetched_count = len(records)
        run.upserted_count = len(rows)
        run.status = "ok"
        session.commit()
        return IngestResult(status="ok", fetched=len(records), upserted=len(rows))
    except Exception as exc:
        session.rollback()
        run.status = "error"
        run.finished_at = datetime.now(UTC)
        run.error_text = str(exc)[:2000]
        session.add(run)
        session.commit()
        raise


def run_uk_ingest(session: Session) -> IngestResult:
    run = IngestRun(source="uk_fsa", status="running")
    session.add(run)
    session.commit()
    try:
        records = fetch_fsa()
        rows = _dedupe(normalize_fsa(record) for record in records)
        _upsert_recalls(session, rows)
        run.finished_at = datetime.now(UTC)
        run.fetched_count = len(records)
        run.upserted_count = len(rows)
        run.status = "ok"
        session.commit()
        return IngestResult(status="ok", fetched=len(records), upserted=len(rows))
    except Exception as exc:
        session.rollback()
        run.status = "error"
        run.finished_at = datetime.now(UTC)
        run.error_text = str(exc)[:2000]
        session.add(run)
        session.commit()
        raise


def run_ingest(session: Session, limit: int = 1000) -> IngestResult:
    run = IngestRun(source="openfda_food", status="running")
    session.add(run)
    session.commit()

    try:
        records = fetch_enforcement(limit)
        rows = _dedupe(map(normalize_recall, records))
        _upsert_recalls(session, rows)
        run.finished_at = datetime.now(UTC)
        run.fetched_count = len(records)
        run.upserted_count = len(rows)
        run.status = "ok"
        session.commit()
        return IngestResult(status="ok", fetched=len(records), upserted=len(rows))
    except Exception as exc:
        session.rollback()
        run.status = "error"
        run.finished_at = datetime.now(UTC)
        run.error_text = _redact_secrets(str(exc))
        session.add(run)
        session.commit()
        raise
