from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import TableValuedAlias

from app.config import settings
from app.modules.recalls.anomalies import detect_anomalies
from app.modules.recalls.forecast import forecast_series
from app.modules.recalls.fsa_uk import fetch_fsa, normalize_fsa
from app.modules.recalls.fsis import fetch_fsis, normalize_fsis
from app.modules.recalls.models import (
    IngestRun,
    Recall,
    RecallEvent,
    RecallNeighbor,
    RecallStatsCache,
    RecallTopic,
)
from app.modules.recalls.ncc_za import fetch_ncc, normalize_ncc
from app.modules.recalls.normalize import NormalizedRecall
from app.modules.recalls.openfda import fetch_enforcement, normalize_recall
from app.modules.recalls.schemas import (
    Anomaly,
    AnomalyMonth,
    AnomalyScope,
    CategoryCount,
    EntityCount,
    EventOut,
    ForecastPoint,
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
from app.modules.recalls.seed_za import fetch_seed, normalize_seed

# Rows per upsert statement — keeps a large backfill to a few statements instead of thousands.
_UPSERT_CHUNK = 500

# How many rows to return for the high-cardinality company breakdown.
_TOP_N = 15

# Recalls are identified by (source, recall_number) — the dedupe + upsert conflict key.
_CONFLICT_KEYS = ("source", "recall_number")

# Which ingest sources belong to each country — scopes the "last updated" timestamp and drives the
# per-country stats rebuild loop (rebuild_stats), so a new country must be registered here.
_COUNTRY_SOURCES = {
    "us": ("openfda_food", "usda_fsis"),
    "uk": ("uk_fsa",),
    "za": ("ncc_za", "seed_za"),
}

# Anomaly scan: how many top entities to monitor, how many flags to surface, and the recency window
# we surface them from — current trends matter more than a big spike from a decade ago.
_ANOMALY_TOP_ENTITIES = 20
_ANOMALY_LIMIT = 8
_ANOMALY_RECENT_MONTHS = 24

# Severity bands surface worst-first in the stats breakdown (label is text, so we order it here).
_SEVERITY_RANK = {
    SeverityLabel.severe.value: 0,
    SeverityLabel.high.value: 1,
    SeverityLabel.moderate.value: 2,
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
    topic: str | None = None,
    event: str | None = None,
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
        # Exact severity band: low / moderate / high / severe.
        conditions.append(Recall.severity_label == severity)
    if topic:
        # Resolve the theme slug to its surrogate id(s), scoped to the country when set — slugs are
        # unique per country, so the same theme in another country is a different row.
        topic_ids = select(RecallTopic.id).where(RecallTopic.slug == topic)
        if country:
            topic_ids = topic_ids.where(RecallTopic.country == country)
        conditions.append(Recall.topic_id.in_(topic_ids))
    if event:
        # Same pattern for the event/outbreak cluster slug → its surrogate id(s), country-scoped.
        event_ids = select(RecallEvent.id).where(RecallEvent.slug == event)
        if country:
            event_ids = event_ids.where(RecallEvent.country == country)
        conditions.append(Recall.event_cluster_id.in_(event_ids))
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
    topic: str | None = None,
    event: str | None = None,
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
        event=event,
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


def compute_stats(session: Session, country: str | None = None) -> RecallStats:
    """Compute the full stats payload live from `recalls` — the expensive path.

    Runs ~8 aggregations + the anomaly scan over many series + the forecast. In production this is
    materialized per country (see `rebuild_stats`) and served by `get_stats` as a single row read;
    it is called directly only by the offline rebuild and as `get_stats`'s fallback.
    """

    def scoped(stmt):
        # US and UK are shown separately, so every aggregation is scoped to the chosen country.
        return stmt.where(Recall.country == country) if country else stmt

    # A secondary sort on the label breaks count ties so each leaderboard is ordered the same way
    # run-to-run — the materialized payload then equals a live recompute, and the `limit`ed
    # breakdowns below pick a deterministic top-N instead of an arbitrary one among tied rows.
    by_category = session.execute(
        scoped(
            select(Recall.category, func.count())
            .group_by(Recall.category)
            .order_by(func.count().desc(), Recall.category)
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
            .order_by(func.count().desc(), states_elem.c.value)
        )
    ).all()
    by_company = session.execute(
        scoped(
            select(Recall.company_name, func.count())
            .where(Recall.company_name.is_not(None))
            .group_by(Recall.company_name)
            .order_by(func.count().desc(), Recall.company_name)
            .limit(_TOP_N)
        )
    ).all()
    by_source = session.execute(
        scoped(
            select(Recall.source, func.count())
            .group_by(Recall.source)
            .order_by(func.count().desc(), Recall.source)
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
            .order_by(func.count().desc(), entity_type, entity_value)
        )
    ).all()

    # Anomaly scan: robust z-score over the monthly series for overall volume, each category, and
    # the busiest entities. Cheap on ~29k rows; the highest-|z| flags surface as trend callouts.
    calendar = _continuous_months([m for m, _ in by_month])
    anomalies: list[Anomaly] = []
    forecast: list[ForecastPoint] = []
    if calendar:
        candidates: list[Anomaly] = []

        def _add(scope: AnomalyScope, label: str, counts: list[tuple[str, int]]) -> None:
            found = _scope_anomaly(scope, label, counts)
            if found is not None:
                candidates.append(found)

        overall_counts = {m: c for m, c in by_month}
        overall_series = [(m, overall_counts.get(m, 0)) for m in calendar]
        # Project overall volume forward from the same gap-filled series the anomaly scan reads.
        forecast = [
            ForecastPoint(
                month=point["month"],
                predicted=point["predicted"],
                lower=point["lower"],
                upper=point["upper"],
            )
            for point in forecast_series(overall_series)
        ]
        _add(AnomalyScope.overall, "All recalls", overall_series)

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
        forecast=forecast,
        last_ingest_at=last_ingest_at,
    )


def get_stats(session: Session, country: str | None = None) -> RecallStats:
    """Return the stats payload, served from the materialized `recall_stats` row when present.

    `rebuild_stats` precomputes a row per dashboard country after each ingest, so the request path
    reads one row instead of recomputing everything `compute_stats` does. Falls back to a live
    `compute_stats` for the country=None ("all") view — which the dashboard never requests, so it
    isn't materialized — and for any country whose row hasn't been built yet (e.g. a freshly
    migrated DB). A missing row therefore degrades to slow, never to an error.
    """
    if country is not None:
        row = session.get(RecallStatsCache, country)
        if row is not None:
            return RecallStats.model_validate(row.payload)
    return compute_stats(session, country)


def rebuild_stats(session: Session) -> dict[str, int]:
    """Materialize the stats payload for each dashboard country into `recall_stats`.

    Compute every payload first, then delete + insert in one transaction, so a failure mid-compute
    never touches the stored rows — the old cache survives untouched and `get_stats` keeps serving
    it. The payload is stored as `model_dump(mode="json")` so datetimes (last_ingest_at) serialize
    into JSONB; `get_stats` reverses it with `model_validate` (CamelModel has populate_by_name, so
    snake-case round-trips).
    """
    now = datetime.now(UTC)
    countries = list(_COUNTRY_SOURCES)  # the per-country scopes the dashboard requests: us, uk
    payloads = {
        country: compute_stats(session, country).model_dump(mode="json") for country in countries
    }
    session.execute(delete(RecallStatsCache))
    for country in countries:
        session.add(RecallStatsCache(country=country, payload=payloads[country], computed_at=now))
    session.commit()
    return {"countries": len(countries)}


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
    topic: str | None = None,
    event: str | None = None,
    since: date | None = None,
    until: date | None = None,
    search: str | None = None,
) -> TrendResult:
    # Monthly counts, optionally split by category / source / severity / classification, scoped by
    # the same filters as the recall list — so the chart and the list always describe the same set.
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
            event=event,
            since=since,
            until=until,
            search=search,
        ),
    ]
    month = func.to_char(Recall.report_date, "YYYY-MM")
    dimension = {
        TrendGroup.category.value: Recall.category,
        TrendGroup.source.value: Recall.source,
        TrendGroup.severity.value: Recall.severity_label,
        # classification is nullable — label the unset rows so they form their own segment.
        TrendGroup.classification.value: func.coalesce(Recall.classification, "Unclassified"),
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


def get_topics(session: Session, country: str | None = None) -> list[TopicOut]:
    # The materialised NMF themes for a country, largest first; empty topics are hidden.
    stmt = select(RecallTopic).where(RecallTopic.size > 0)
    if country:
        stmt = stmt.where(RecallTopic.country == country)
    rows = session.scalars(stmt.order_by(RecallTopic.size.desc())).all()
    return [
        TopicOut(id=row.id, slug=row.slug, label=row.label, top_terms=row.top_terms, size=row.size)
        for row in rows
    ]


def get_events(
    session: Session, country: str | None = None, *, outbreaks_only: bool = False
) -> list[EventOut]:
    # Materialised event/outbreak clusters for a country, outbreaks first then by recall count, so
    # the dashboard headlines the high-signal incidents.
    stmt = select(RecallEvent)
    if country:
        stmt = stmt.where(RecallEvent.country == country)
    if outbreaks_only:
        stmt = stmt.where(RecallEvent.is_outbreak.is_(True))
    rows = session.scalars(
        stmt.order_by(RecallEvent.is_outbreak.desc(), RecallEvent.recall_count.desc())
    ).all()
    return [
        EventOut(
            id=row.id,
            slug=row.slug,
            label=row.label,
            is_outbreak=row.is_outbreak,
            dominant_entity=row.dominant_entity,
            recall_count=row.recall_count,
            company_count=row.company_count,
            state_count=row.state_count,
            first_date=row.first_date,
            last_date=row.last_date,
            severity_max=row.severity_max,
        )
        for row in rows
    ]


def get_recall(session: Session, source: str, recall_number: str) -> RecallOut | None:
    # One recall by its composite PK (source, recall_number) — backs the recall detail page.
    recall = session.get(Recall, (source, recall_number))
    return RecallOut.model_validate(recall) if recall is not None else None


def get_similar(
    session: Session, source: str, recall_number: str, limit: int = 6
) -> list[SimilarRecall]:
    # Precomputed nearest neighbours for one recall, rank-ordered, hydrated to full recalls. The
    # stored set is already capped per recall, so fetch them all (not just `limit`) and trim after
    # dropping deleted ones — limiting the query first could return fewer than `limit` valid rows
    # when a top-ranked neighbour's recall was removed since the build.
    neighbors = session.scalars(
        select(RecallNeighbor)
        .where(RecallNeighbor.source == source, RecallNeighbor.recall_number == recall_number)
        .order_by(RecallNeighbor.rank)
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
        if len(out) == limit:
            break
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
        # up only this batch's own composite keys, not the whole source, and compare on the full
        # (source, recall_number) so the count holds for a mixed-source job too. Chunk the lookup
        # like the upsert below: a single row-wise IN over a full-history backfill's ~26k keys
        # expands to a deeply nested OR tree that overflows Postgres' max_stack_depth.
        keys = [(row["source"], row["recall_number"]) for row in rows]
        existing: set[tuple[str, str]] = set()
        for start in range(0, len(keys), _UPSERT_CHUNK):
            chunk = keys[start : start + _UPSERT_CHUNK]
            existing.update(
                (src, num)
                for src, num in session.execute(
                    select(Recall.source, Recall.recall_number).where(
                        tuple_(Recall.source, Recall.recall_number).in_(chunk)
                    )
                )
            )
        new_count = sum(1 for row in rows if (row["source"], row["recall_number"]) not in existing)
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


def run_ncc_ingest(session: Session) -> IngestResult:
    return _run_ingest_job(session, source="ncc_za", fetch=fetch_ncc, normalize=normalize_ncc)


def run_seed_ingest(session: Session) -> IngestResult:
    # Curated SA recalls NCC doesn't carry (Woolworths/Shoprite/NRCS) — see seed_za.py.
    return _run_ingest_job(session, source="seed_za", fetch=fetch_seed, normalize=normalize_seed)
