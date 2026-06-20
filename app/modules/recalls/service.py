from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import TableValuedAlias

from app.config import settings
from app.modules.recalls.anomalies import detect_anomalies
from app.modules.recalls.fsa_uk import fetch_fsa, normalize_fsa
from app.modules.recalls.fsis import fetch_fsis, normalize_fsis
from app.modules.recalls.models import IngestRun, Recall, RecallNeighbor, RecallTopic
from app.modules.recalls.normalize import NormalizedRecall
from app.modules.recalls.openfda import fetch_enforcement, normalize_recall
from app.modules.recalls.schemas import (
    Anomaly,
    AnomalyMonth,
    AnomalyScope,
    CategoryCount,
    EntityCount,
    IngestResult,
    LabelCount,
    MonthCount,
    RecallListResult,
    RecallOut,
    RecallSort,
    RecallStats,
    SeverityLabel,
    SimilarRecall,
    TopicOut,
    TrendBucket,
    TrendGroup,
    TrendResult,
)

# Rows per upsert statement — keeps a large backfill to a few statements instead of thousands.
_UPSERT_CHUNK = 500

# How many rows to return for the high-cardinality company breakdown.
_TOP_N = 15

# Recalls are identified by (source, recall_number) — the dedupe + upsert conflict key.
_CONFLICT_KEYS = ("source", "recall_number")

# Which ingest sources belong to each country — scopes the "last updated" timestamp.
_COUNTRY_SOURCES = {"us": ("openfda_food", "usda_fsis"), "uk": ("uk_fsa",)}

# Anomaly scan: how many top entities to monitor, how many flags to surface, and the recency window
# we surface them from — current trends matter more than a big spike from a decade ago.
_ANOMALY_TOP_ENTITIES = 20
_ANOMALY_LIMIT = 8
_ANOMALY_RECENT_MONTHS = 24

# Severity bands surface worst-first in the stats breakdown (label is text, so we order it here).
_SEVERITY_RANK = {
    SeverityLabel.severe.value: 0,
    SeverityLabel.high.value: 1,
    SeverityLabel.elevated.value: 2,
    SeverityLabel.low.value: 3,
}


def _continuous_months(months: list[str]) -> list[str]:
    # Fill the calendar between the first and last present month so the anomaly baseline sees
    # zero-recall months instead of treating sparse months as adjacent.
    if not months:
        return []
    year, month = int(months[0][:4]), int(months[0][5:7])
    end = (int(months[-1][:4]), int(months[-1][5:7]))
    out: list[str] = []
    while (year, month) <= end:
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def _scope_anomaly(
    scope: AnomalyScope, label: str, series: list[tuple[str, int]]
) -> Anomaly | None:
    # One consolidated card per thing: all its flagged months + the window to chart them against.
    points = detect_anomalies(series)
    if not points:
        return None
    window = [MonthCount(month=m, count=c) for m, c in series[-_ANOMALY_RECENT_MONTHS:]]
    months = [
        AnomalyMonth(
            month=point["month"],
            observed=point["observed"],
            baseline=point["baseline"],
            z=point["z"],
        )
        for point in points
    ]
    return Anomaly(scope=scope, label=label, months=months, series=window)


def _surface_anomalies(
    candidates: list[Anomaly], recent_months: set[str], limit: int
) -> list[Anomaly]:
    # Emphasize current trends: keep only each thing's recent flagged months, drop things with none,
    # rank by peak severity (so the slots go to distinct things), then present newest-first.
    surfaced: list[Anomaly] = []
    for candidate in candidates:
        recent = [month for month in candidate.months if month.month in recent_months]
        if recent:
            surfaced.append(candidate.model_copy(update={"months": recent}))
    surfaced.sort(key=lambda a: max(abs(month.z) for month in a.months), reverse=True)
    surfaced = surfaced[:limit]
    surfaced.sort(key=lambda a: max(month.month for month in a.months), reverse=True)
    return surfaced


def _dedupe(rows: Iterable[NormalizedRecall]) -> list[NormalizedRecall]:
    # Keep the last row per identity so a multi-row upsert can't touch the same PK twice.
    return list({(r["source"], r["recall_number"]): r for r in rows}.values())


def _upsert_recalls(session: Session, rows: list[NormalizedRecall]) -> None:
    for start in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[start : start + _UPSERT_CHUNK]
        insert_stmt = pg_insert(Recall).values(chunk)
        update_set: dict[str, Any] = {
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


def _recall_conditions(
    *,
    country: str | None = None,
    source: str | None = None,
    category: str | None = None,
    classification: str | None = None,
    state: str | None = None,
    company: str | None = None,
    entity: str | None = None,
    min_severity: float | None = None,
    severity: str | None = None,
    topic: int | None = None,
    since: date | None = None,
    until: date | None = None,
    search: str | None = None,
) -> list[Any]:
    # The shared filter set behind both the recall list and the trend chart, so the two scope
    # identically. Each arg is optional; an omitted one adds no constraint.
    conditions: list[Any] = []
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
        # slowly, so it stays cheap for a long time; revisit with a pg_trgm GIN index if it grows.
        conditions.append(Recall.company_name.ilike(f"%{company}%"))
    if entity:
        # `entities` is the array of {type, value}; match if any element has this value (GIN @>).
        conditions.append(Recall.entities.contains([{"value": entity}]))
    if min_severity is not None:
        # Keep recalls at or above a severity floor — backed by the btree index on severity_score.
        conditions.append(Recall.severity_score >= min_severity)
    if severity:
        # Exact severity band: low / elevated / high / severe.
        conditions.append(Recall.severity_label == severity)
    if topic is not None:
        # NMF theme — topic 0 is valid, so test against None, not falsiness.
        conditions.append(Recall.topic_id == topic)
    if since:
        conditions.append(Recall.report_date >= since)
    if until:
        conditions.append(Recall.report_date <= until)
    if search and search.strip():
        conditions.append(
            Recall.search_vector.op("@@")(func.websearch_to_tsquery("english", search.strip()))
        )
    return conditions


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
    entity: str | None = None,
    min_severity: float | None = None,
    severity: str | None = None,
    topic: int | None = None,
    since: date | None = None,
    until: date | None = None,
    search: str | None = None,
    sort: str | None = None,
) -> RecallListResult:
    # Treat blank/whitespace-only as "no search" so it doesn't build a no-op tsquery + ranking.
    search = search.strip() if search else None

    conditions = _recall_conditions(
        country=country,
        source=source,
        category=category,
        classification=classification,
        state=state,
        company=company,
        entity=entity,
        min_severity=min_severity,
        severity=severity,
        topic=topic,
        since=since,
        until=until,
        search=search,
    )

    stmt = select(Recall)
    count_stmt = select(func.count()).select_from(Recall)
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    # Rank by text relevance when searching, then by severity if asked, then most-recent-first so
    # ties always resolve to a stable, sensible order.
    ordering = []
    if search:
        ordering.append(
            func.ts_rank(Recall.search_vector, func.websearch_to_tsquery("english", search)).desc()
        )
    if sort == RecallSort.severity.value:
        ordering.append(Recall.severity_score.desc().nulls_last())
    ordering.append(Recall.report_date.desc().nulls_last())
    rows = session.scalars(stmt.order_by(*ordering).limit(limit).offset(offset)).all()
    total = session.scalar(count_stmt) or 0

    return RecallListResult(items=[RecallOut.model_validate(row) for row in rows], total=total)


def _entities_unnest() -> TableValuedAlias:
    # Unnest the JSONB `entities` array-of-objects into one row per element, so a recall touching
    # several entities counts toward each. Caller reads object fields with `.c.value.op("->>")`.
    return func.jsonb_array_elements(Recall.entities).table_valued("value", joins_implicitly=True)


def get_stats(session: Session, country: str | None = None) -> RecallStats:
    def scoped(stmt):
        # US and UK are shown separately, so every aggregation is scoped to the chosen country.
        return stmt.where(Recall.country == country) if country else stmt

    by_category = session.execute(
        scoped(
            select(Recall.category, func.count())
            .group_by(Recall.category)
            .order_by(func.count().desc())
        )
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
    # Counts per severity band, ordered worst-first in Python (the label is text, not ordinal).
    by_severity = sorted(
        session.execute(
            scoped(select(Recall.severity_label, func.count()).group_by(Recall.severity_label))
        ).all(),
        key=lambda row: _SEVERITY_RANK.get(row[0], len(_SEVERITY_RANK)),
    )
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
    # Count each (type, value), so a recall touching several allergens counts toward each.
    entity_elem = _entities_unnest()
    entity_type = entity_elem.c.value.op("->>")("type")
    entity_value = entity_elem.c.value.op("->>")("value")
    by_entity = session.execute(
        scoped(
            select(entity_type, entity_value, func.count())
            .select_from(Recall, entity_elem)
            .where(func.jsonb_typeof(Recall.entities) == "array")
            .group_by(entity_type, entity_value)
            .order_by(func.count().desc())
        )
    ).all()

    # Anomaly scan: robust z-score over the monthly series for overall volume, each category, and
    # the busiest entities. Cheap on ~29k rows; the highest-|z| flags surface as trend callouts.
    calendar = _continuous_months([m for m, _ in by_month])
    anomalies: list[Anomaly] = []
    if calendar:
        candidates: list[Anomaly] = []

        def _add(scope: AnomalyScope, label: str, counts: list[tuple[str, int]]) -> None:
            found = _scope_anomaly(scope, label, counts)
            if found is not None:
                candidates.append(found)

        overall_counts = {m: c for m, c in by_month}
        _add(AnomalyScope.overall, "All recalls", [(m, overall_counts.get(m, 0)) for m in calendar])

        cat_rows = session.execute(
            scoped(
                select(Recall.category, month.label("month"), func.count())
                .where(Recall.report_date.is_not(None))
                .group_by(Recall.category, month)
            )
        ).all()
        cat_counts: dict[tuple[str, str], int] = {}
        categories: set[str] = set()
        for category, month_label, count in cat_rows:
            cat_counts[(category, month_label)] = count
            categories.add(category)
        for category in categories:
            _add(
                AnomalyScope.category,
                category,
                [(m, cat_counts.get((category, m), 0)) for m in calendar],
            )

        ent_elem = _entities_unnest()
        ent_value = ent_elem.c.value.op("->>")("value")
        ent_rows = session.execute(
            scoped(
                select(ent_value, month.label("month"), func.count())
                .select_from(Recall, ent_elem)
                .where(func.jsonb_typeof(Recall.entities) == "array")
                .where(Recall.report_date.is_not(None))
                .group_by(ent_value, month)
            )
        ).all()
        ent_counts: dict[tuple[str, str], int] = {}
        ent_total: dict[str, int] = {}
        for value, month_label, count in ent_rows:
            ent_counts[(value, month_label)] = count
            ent_total[value] = ent_total.get(value, 0) + count
        for value in sorted(ent_total, key=lambda v: ent_total[v], reverse=True)[
            :_ANOMALY_TOP_ENTITIES
        ]:
            _add(AnomalyScope.entity, value, [(m, ent_counts.get((value, m), 0)) for m in calendar])

        recent_months = set(calendar[-_ANOMALY_RECENT_MONTHS:])
        anomalies = _surface_anomalies(candidates, recent_months, _ANOMALY_LIMIT)

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
        by_severity=[LabelCount(label=label, count=count) for label, count in by_severity],
        by_state=[LabelCount(label=label, count=count) for label, count in by_state],
        by_company=[LabelCount(label=label, count=count) for label, count in by_company],
        by_source=[LabelCount(label=label, count=count) for label, count in by_source],
        by_entity=[
            EntityCount(type=etype, label=label, count=count) for etype, label, count in by_entity
        ],
        anomalies=anomalies,
        last_ingest_at=last_ingest_at,
    )


def get_trend(
    session: Session,
    country: str | None = None,
    group: str = "total",
    *,
    category: str | None = None,
    classification: str | None = None,
    state: str | None = None,
    company: str | None = None,
    source: str | None = None,
    entity: str | None = None,
    min_severity: float | None = None,
    severity: str | None = None,
    topic: int | None = None,
    since: date | None = None,
    until: date | None = None,
    search: str | None = None,
) -> TrendResult:
    # Monthly counts, optionally split by category or source, scoped by the same filters as the
    # recall list — so the chart and the list below it always describe the same set of recalls.
    where = [
        Recall.report_date.is_not(None),
        *_recall_conditions(
            country=country,
            category=category,
            classification=classification,
            state=state,
            company=company,
            source=source,
            entity=entity,
            min_severity=min_severity,
            severity=severity,
            topic=topic,
            since=since,
            until=until,
            search=search,
        ),
    ]
    month = func.to_char(Recall.report_date, "YYYY-MM")
    dimension = {
        TrendGroup.category.value: Recall.category,
        TrendGroup.source.value: Recall.source,
    }.get(group)
    if dimension is not None:
        rows = session.execute(
            select(month.label("month"), dimension, func.count())
            .where(*where)
            .group_by(month, dimension)
            .order_by(month)
        ).all()
        buckets = [TrendBucket(month=m, group=key, count=count) for m, key, count in rows]
    else:
        rows = session.execute(
            select(month.label("month"), func.count()).where(*where).group_by(month).order_by(month)
        ).all()
        buckets = [TrendBucket(month=m, group="total", count=count) for m, count in rows]

    return TrendResult(group=TrendGroup(group), buckets=buckets)


def get_topics(session: Session) -> list[TopicOut]:
    # The materialised NMF themes, largest first; empty topics (no recalls) are hidden.
    rows = session.scalars(
        select(RecallTopic).where(RecallTopic.size > 0).order_by(RecallTopic.size.desc())
    ).all()
    return [
        TopicOut(id=row.id, label=row.label, top_terms=row.top_terms, size=row.size) for row in rows
    ]


def get_similar(
    session: Session, source: str, recall_number: str, limit: int = 6
) -> list[SimilarRecall]:
    # Precomputed nearest neighbours for one recall, rank-ordered, hydrated to full recalls.
    neighbors = session.scalars(
        select(RecallNeighbor)
        .where(RecallNeighbor.source == source, RecallNeighbor.recall_number == recall_number)
        .order_by(RecallNeighbor.rank)
        .limit(limit)
    ).all()
    if not neighbors:
        return []
    # Hydrate every neighbour in one round-trip keyed by the composite PK, then re-attach in rank
    # order. A neighbour can be missing if its recall was deleted since the build — skip those.
    keys = [(n.neighbor_source, n.neighbor_number) for n in neighbors]
    by_key = {
        (recall.source, recall.recall_number): recall
        for recall in session.scalars(
            select(Recall).where(tuple_(Recall.source, Recall.recall_number).in_(keys))
        )
    }
    out: list[SimilarRecall] = []
    for neighbor in neighbors:
        recall = by_key.get((neighbor.neighbor_source, neighbor.neighbor_number))
        if recall is not None:
            out.append(
                SimilarRecall(similarity=neighbor.score, recall=RecallOut.model_validate(recall))
            )
    return out


def search_companies(
    session: Session, country: str | None = None, q: str = "", limit: int = 30
) -> list[str]:
    # Distinct company names matching `q` (case-insensitive substring), ranked by recall count —
    # powers the company filter's type-ahead so any of the thousands of firms is reachable, not just
    # the top handful in the stats breakdown.
    stmt = select(Recall.company_name).where(Recall.company_name.is_not(None))
    if country:
        stmt = stmt.where(Recall.country == country)
    term = q.strip()
    if term:
        stmt = stmt.where(Recall.company_name.ilike(f"%{term}%"))
    stmt = stmt.group_by(Recall.company_name).order_by(func.count().desc()).limit(limit)
    return [name for name in session.scalars(stmt).all() if name is not None]


def _run_ingest_job(
    session: Session,
    *,
    source: str,
    fetch: Callable[[], list[Any]],
    normalize: Callable[[Any], NormalizedRecall],
) -> IngestResult:
    run = IngestRun(source=source, status="running")
    session.add(run)
    session.commit()
    try:
        records = fetch()
        rows = _dedupe(normalize(record) for record in records)
        # New = rows whose (source, recall_number) isn't already stored. The upsert touches new and
        # re-seen rows alike, so upserted_count alone is almost always just the fetched total. Look
        # up only this batch's own composite keys (bounded by the fetch), not the whole source; a
        # job carries one source, so recall_number alone then dedupes against what came back.
        keys = [(row["source"], row["recall_number"]) for row in rows]
        existing = (
            set(
                session.scalars(
                    select(Recall.recall_number).where(
                        tuple_(Recall.source, Recall.recall_number).in_(keys)
                    )
                )
            )
            if keys
            else set()
        )
        new_count = sum(1 for row in rows if row["recall_number"] not in existing)
        _upsert_recalls(session, rows)
        run.finished_at = datetime.now(UTC)
        run.fetched_count = len(records)
        run.upserted_count = len(rows)
        run.status = "ok"
        session.commit()
        return IngestResult(status="ok", fetched=len(records), new=new_count, upserted=len(rows))
    except Exception as exc:
        session.rollback()
        run.status = "error"
        run.finished_at = datetime.now(UTC)
        # Redact (openFDA exception strings embed ?api_key) before persisting, then bound the
        # length — redact-then-slice so a secret straddling the cutoff can't survive.
        run.error_text = _redact_secrets(str(exc))[:2000]
        session.add(run)
        session.commit()
        raise


def run_fda_ingest(session: Session, limit: int = 1000) -> IngestResult:
    return _run_ingest_job(
        session,
        source="openfda_food",
        fetch=lambda: fetch_enforcement(limit),
        normalize=normalize_recall,
    )


def run_fsis_ingest(session: Session) -> IngestResult:
    return _run_ingest_job(session, source="usda_fsis", fetch=fetch_fsis, normalize=normalize_fsis)


def run_uk_ingest(session: Session) -> IngestResult:
    return _run_ingest_job(session, source="uk_fsa", fetch=fetch_fsa, normalize=normalize_fsa)
