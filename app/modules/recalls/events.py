"""Event / outbreak clustering — groups recalls that are the same underlying incident. No LLM.

Each recall is a node; an edge links two recalls that are plausibly the same incident:
  * they carry the **same FDA event_id** (FDA's own grouping of one enforcement event — a strong
    seed, but FDA-only), or
  * they **share a pathogen, fall within a time window, and read alike** — the last via the Phase 2
    cosine neighbours in `recall_neighbors`. Requiring text similarity (not just a shared pathogen)
    keeps unrelated same-pathogen recalls from collapsing into one blob.

Clusters are the **connected components** of that graph (`scipy.sparse.csgraph`), computed **per
country** like the similarity graph. A cluster is flagged an **outbreak** when it's multi-recall
and pathogen-driven; other multi-recall clusters (e.g. one firm's multi-product cascade, grouped by
event_id) are plain "events". Singletons get no cluster.

Pure compute lives in `cluster_events`; `rebuild_events` does the DB I/O and materialises
`recall_events` + `recalls.event_cluster_id`. Deterministic: same data in → same clusters out.
Reuses scipy (already a dependency) — no new package. See scripts/build_events.py.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import cast

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from sqlalchemy import Table, bindparam, delete, insert, select, update
from sqlalchemy.orm import Session, load_only

from app.modules.recalls.models import Recall, RecallEvent, RecallNeighbor
from app.modules.recalls.schemas import EntityType

# Linkage thresholds — tuned empirically on the real corpus (see git history). A pair links on text
# only above this cosine score, and only if their report dates are within the window. Kept fairly
# tight: pathogen reason-text is boilerplate-heavy ("possible Salmonella contamination"), so a loose
# score chains distinct outbreak waves into one blob via transitive links.
_MIN_SCORE = 0.50
_WINDOW_DAYS = 60

# A cluster needs ≥ this many recalls to exist at all; an outbreak needs ≥ this many AND a shared
# pathogen. Singletons (and below the cluster floor) get no event.
_MIN_CLUSTER_SIZE = 2
_MIN_OUTBREAK_SIZE = 3

# A pathogen must be shared by at least this many members to count as the cluster's driver (so one
# stray pathogen tag on an allergen cascade doesn't mislabel it an outbreak).
_MIN_SHARED_PATHOGEN = 2

_PATHOGEN = EntityType.pathogen.value
_DB_CHUNK = 1000


@dataclass
class EventInput:
    """The minimal per-recall view the clusterer needs — DB-free, so cluster_events is unit-testable
    without a database (mirrors build_analytics taking plain text)."""

    pathogens: frozenset[str]  # pathogen entity values, for the link rule + outbreak driver
    entities: tuple[str, ...]  # all entity values, for the dominant-entity fallback
    report_date: date | None
    company: str | None
    states: tuple[str, ...]
    severity: float
    event_id: str | None  # FDA's raw grouping field (None for USDA/UK)


@dataclass
class EventCluster:
    members: list[int]  # indices into the input list
    label: str
    is_outbreak: bool
    dominant_entity: str | None
    company_count: int
    state_count: int
    first_date: date | None
    last_date: date | None
    severity_max: float


@dataclass
class EventResult:
    # cluster_ids[i] indexes into `clusters` (or None when recall i joins no cluster).
    cluster_ids: list[int | None]
    clusters: list[EventCluster] = field(default_factory=list)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _date_close(a: date | None, b: date | None, window_days: int) -> bool:
    # A missing date can't satisfy the window — such recalls only cluster via a shared event_id.
    return a is not None and b is not None and abs((a - b).days) <= window_days


def _build_cluster(
    members: list[int], recalls: list[EventInput], min_outbreak_size: int
) -> EventCluster:
    rows = [recalls[m] for m in members]
    pathogen_counts = Counter(value for r in rows for value in r.pathogens)
    # The outbreak driver is the most common pathogen, but only if genuinely shared.
    dominant_pathogen = None
    if pathogen_counts:
        value, count = pathogen_counts.most_common(1)[0]
        if count >= _MIN_SHARED_PATHOGEN:
            dominant_pathogen = value
    entity_counts = Counter(value for r in rows for value in r.entities)
    dominant_entity = dominant_pathogen or (
        entity_counts.most_common(1)[0][0] if entity_counts else None
    )

    companies = {r.company for r in rows if r.company}
    states = {state for r in rows for state in r.states}
    dates = [r.report_date for r in rows if r.report_date]

    if dominant_entity:
        head: str | None = dominant_entity
    elif len(companies) == 1:
        head = next(iter(companies))
    else:
        head = None
    label = f"{head} · {len(members)} recalls" if head else f"{len(members)} related recalls"

    return EventCluster(
        members=members,
        label=label,
        is_outbreak=len(members) >= min_outbreak_size and dominant_pathogen is not None,
        dominant_entity=dominant_entity,
        company_count=len(companies),
        state_count=len(states),
        first_date=min(dates) if dates else None,
        last_date=max(dates) if dates else None,
        severity_max=round(max((r.severity for r in rows), default=0.0), 1),
    )


def cluster_events(
    recalls: list[EventInput],
    neighbor_edges: list[tuple[int, int, float]],
    *,
    min_score: float = _MIN_SCORE,
    window_days: int = _WINDOW_DAYS,
    min_cluster_size: int = _MIN_CLUSTER_SIZE,
    min_outbreak_size: int = _MIN_OUTBREAK_SIZE,
) -> EventResult:
    """Cluster `recalls` into events from the cosine-neighbour edges + shared event_id, and flag the
    pathogen-driven multi-recall ones as outbreaks. `neighbor_edges` are (i, j, cosine) pairs."""
    count = len(recalls)
    cluster_ids: list[int | None] = [None] * count
    if count == 0:
        return EventResult(cluster_ids=cluster_ids)

    rows: list[int] = []
    cols: list[int] = []

    # (A) text-similarity links: a cosine neighbour pair that shares a pathogen within the window.
    for i, j, score in neighbor_edges:
        if (
            score >= min_score
            and recalls[i].pathogens & recalls[j].pathogens
            and _date_close(recalls[i].report_date, recalls[j].report_date, window_days)
        ):
            rows.append(i)
            cols.append(j)

    # (B) FDA event_id links: recalls sharing a non-null event_id are one enforcement event. A star
    # from the first member to the rest is enough to put them in one connected component.
    by_event: dict[str, list[int]] = {}
    for index, recall in enumerate(recalls):
        if recall.event_id:
            by_event.setdefault(recall.event_id, []).append(index)
    for grouped in by_event.values():
        for other in grouped[1:]:
            rows.append(grouped[0])
            cols.append(other)

    # Connected components over the undirected graph (symmetrise; duplicate edges are harmless).
    symmetric_rows = np.array(rows + cols, dtype=np.int64)
    symmetric_cols = np.array(cols + rows, dtype=np.int64)
    data = np.ones(symmetric_rows.shape[0], dtype=np.int8)
    adjacency = coo_matrix((data, (symmetric_rows, symmetric_cols)), shape=(count, count))
    _, labels = connected_components(adjacency, directed=False)

    components: dict[int, list[int]] = {}
    for index, component in enumerate(labels):
        components.setdefault(int(component), []).append(index)

    clusters: list[EventCluster] = []
    for members in components.values():
        if len(members) < min_cluster_size:
            continue
        cluster_index = len(clusters)
        clusters.append(_build_cluster(members, recalls, min_outbreak_size))
        for member in members:
            cluster_ids[member] = cluster_index
    return EventResult(cluster_ids=cluster_ids, clusters=clusters)


def _to_input(recall: Recall) -> EventInput:
    # Skip any stored entity missing its value/type so one malformed row can't abort the rebuild.
    entities = [e for e in (recall.entities or []) if e.get("value") and e.get("type")]
    return EventInput(
        pathogens=frozenset(e["value"] for e in entities if e["type"] == _PATHOGEN),
        entities=tuple(e["value"] for e in entities),
        report_date=recall.report_date,
        company=recall.company_name,
        states=tuple(recall.states or ()),
        severity=recall.severity_score or 0.0,
        event_id=recall.event_id,
    )


def _unique_slug(cluster: EventCluster, seen: set[str]) -> str:
    # Stable, readable per-country key: dominant entity + the incident's first month, e.g.
    # "listeria-2026-03". Disambiguated on collision, like analytics' topic slugs.
    head = cluster.dominant_entity or "event"
    month = f"{cluster.first_date:%Y-%m}" if cluster.first_date else ""
    base = _slugify(f"{head} {month}") or "event"
    slug, suffix = base, 2
    while slug in seen:
        slug, suffix = f"{base}-{suffix}", suffix + 1
    seen.add(slug)
    return slug


def rebuild_events(session: Session) -> dict[str, int]:
    """Recompute event clusters and replace the materialised table. Clustering runs **per country**
    (like the similarity graph it reuses), with surrogate ids unique across countries so
    recalls.event_cluster_id stays one int. Called by scripts/build_events.py. One transaction."""
    recalls = list(
        session.scalars(
            select(Recall)
            .options(
                load_only(
                    Recall.source,
                    Recall.recall_number,
                    Recall.country,
                    Recall.event_id,
                    Recall.entities,
                    Recall.company_name,
                    Recall.report_date,
                    Recall.states,
                    Recall.severity_score,
                )
            )
            .order_by(Recall.country, Recall.source, Recall.recall_number)
        ).all()
    )
    # The Phase 2 cosine neighbour edges — already within-country, so cross-country pairs won't
    # resolve against a country's index below.
    neighbor_edges = session.execute(
        select(
            RecallNeighbor.source,
            RecallNeighbor.recall_number,
            RecallNeighbor.neighbor_source,
            RecallNeighbor.neighbor_number,
            RecallNeighbor.score,
        )
    ).all()

    by_country: dict[str, list[Recall]] = {}
    for recall in recalls:
        by_country.setdefault(recall.country, []).append(recall)

    session.execute(delete(RecallEvent))
    session.flush()

    event_rows: list[dict[str, object]] = []
    # Collect every recall's new cluster id and write them in one bulk UPDATE at the end (below)
    # rather than mutating the ORM rows — event_cluster_id is a derived field, so its write must NOT
    # bump recalls.updated_at (the "source data changed" signal build_analytics/build_stats read).
    cluster_ids: list[dict[str, object]] = []
    next_id = 0  # surrogate ids, unique across countries so recalls.event_cluster_id stays one int
    for country in sorted(by_country):
        group = by_country[country]
        index_of = {(r.source, r.recall_number): i for i, r in enumerate(group)}
        inputs = [_to_input(r) for r in group]
        edges: list[tuple[int, int, float]] = []
        for src, num, neighbor_src, neighbor_num, score in neighbor_edges:
            i = index_of.get((src, num))
            j = index_of.get((neighbor_src, neighbor_num))
            if i is not None and j is not None and i != j:
                edges.append((i, j, float(score)))

        result = cluster_events(inputs, edges)

        seen_slugs: set[str] = set()
        local_to_global: dict[int, int] = {}
        for cluster_index, cluster in enumerate(result.clusters):
            local_to_global[cluster_index] = next_id
            event_rows.append(
                {
                    "id": next_id,
                    "country": country,
                    "slug": _unique_slug(cluster, seen_slugs),
                    "label": cluster.label,
                    "is_outbreak": cluster.is_outbreak,
                    "dominant_entity": cluster.dominant_entity,
                    "recall_count": len(cluster.members),
                    "company_count": cluster.company_count,
                    "state_count": cluster.state_count,
                    "first_date": cluster.first_date,
                    "last_date": cluster.last_date,
                    "severity_max": cluster.severity_max,
                }
            )
            next_id += 1

        for recall, assigned in zip(group, result.cluster_ids, strict=True):
            cluster_ids.append(
                {
                    "b_source": recall.source,
                    "b_number": recall.recall_number,
                    "b_cluster": local_to_global[assigned] if assigned is not None else None,
                }
            )

    for start in range(0, len(event_rows), _DB_CHUNK):
        session.execute(insert(RecallEvent), event_rows[start : start + _DB_CHUNK])

    # Write event_cluster_id while preserving updated_at: setting updated_at to itself keeps the
    # column in the UPDATE's SET clause, so the onupdate=func.now() default doesn't fire and a
    # cluster-id-only change can't masquerade as a source change to the staleness checks. A Core
    # table UPDATE keeps this a plain parameterised executemany, not an ORM bulk-update-by-PK.
    recall_table = cast(Table, Recall.__table__)
    for start in range(0, len(cluster_ids), _DB_CHUNK):
        session.execute(
            update(recall_table)
            .where(recall_table.c.source == bindparam("b_source"))
            .where(recall_table.c.recall_number == bindparam("b_number"))
            .values(event_cluster_id=bindparam("b_cluster"), updated_at=recall_table.c.updated_at),
            cluster_ids[start : start + _DB_CHUNK],
        )

    session.commit()
    # The Core bulk UPDATE above bypassed the identity map, so the loaded recalls still hold their
    # old event_cluster_id in memory; expire them so any later read reloads from the DB.
    session.expire_all()
    outbreaks = sum(1 for row in event_rows if row["is_outbreak"])
    return {"recalls": len(recalls), "events": len(event_rows), "outbreaks": outbreaks}
