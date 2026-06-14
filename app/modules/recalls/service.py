from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
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

# How many rows to return for the high-cardinality breakdowns (states, companies).
_TOP_N = 15


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
    category: str | None = None,
    classification: str | None = None,
    state: str | None = None,
    company: str | None = None,
    since: date | None = None,
) -> RecallListResult:
    conditions = []
    if category:
        conditions.append(Recall.category == category)
    if classification:
        conditions.append(Recall.classification == classification)
    if state:
        conditions.append(Recall.state == state)
    if company:
        conditions.append(Recall.company_name.ilike(f"%{company}%"))
    if since:
        conditions.append(Recall.report_date >= since)

    stmt = select(Recall)
    count_stmt = select(func.count()).select_from(Recall)
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    rows = session.scalars(
        stmt.order_by(Recall.report_date.desc().nulls_last()).limit(limit).offset(offset)
    ).all()
    total = session.scalar(count_stmt) or 0

    return RecallListResult(items=[RecallOut.model_validate(row) for row in rows], total=total)


def get_stats(session: Session) -> RecallStats:
    by_category = session.execute(
        select(Recall.category, func.count()).group_by(Recall.category)
    ).all()

    month = func.to_char(Recall.report_date, "YYYY-MM")
    by_month = session.execute(
        select(month.label("month"), func.count())
        .where(Recall.report_date.is_not(None))
        .group_by(month)
        .order_by(month)
    ).all()

    by_classification = session.execute(
        select(Recall.classification, func.count())
        .where(Recall.classification.is_not(None))
        .group_by(Recall.classification)
        .order_by(Recall.classification)
    ).all()
    by_state = session.execute(
        select(Recall.state, func.count())
        .where(Recall.state.is_not(None))
        .group_by(Recall.state)
        .order_by(func.count().desc())
        .limit(_TOP_N)
    ).all()
    by_company = session.execute(
        select(Recall.company_name, func.count())
        .where(Recall.company_name.is_not(None))
        .group_by(Recall.company_name)
        .order_by(func.count().desc())
        .limit(_TOP_N)
    ).all()

    total = session.scalar(select(func.count()).select_from(Recall)) or 0
    last_ingest_at = session.scalar(
        select(IngestRun.finished_at)
        .where(IngestRun.status == "ok")
        .order_by(IngestRun.finished_at.desc())
        .limit(1)
    )

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
        last_ingest_at=last_ingest_at,
    )


def run_ingest(session: Session, limit: int = 1000) -> IngestResult:
    run = IngestRun(source="openfda_food", status="running")
    session.add(run)
    session.commit()

    try:
        records = fetch_enforcement(limit)
        # Dedupe within the batch (keep last) so a multi-row upsert can't touch the same PK twice.
        rows = list({r["recall_number"]: r for r in map(normalize_recall, records)}.values())
        for start in range(0, len(rows), _UPSERT_CHUNK):
            chunk = rows[start : start + _UPSERT_CHUNK]
            insert_stmt = pg_insert(Recall).values(chunk)
            update_set = {
                key: insert_stmt.excluded[key] for key in chunk[0] if key != "recall_number"
            }
            update_set["updated_at"] = datetime.now(UTC)
            session.execute(
                insert_stmt.on_conflict_do_update(index_elements=["recall_number"], set_=update_set)
            )
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
