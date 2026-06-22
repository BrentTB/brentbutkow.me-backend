from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy.orm import Session

from app.auth import require_bearer
from app.db import get_session
from app.modules.recalls.schemas import (
    EventOut,
    IngestResult,
    RecallCategory,
    RecallClass,
    RecallCountry,
    RecallListResult,
    RecallOut,
    RecallSort,
    RecallSource,
    RecallStats,
    SeverityLabel,
    SimilarRecall,
    TopicOut,
    TrendGroup,
    TrendResult,
)
from app.modules.recalls.service import (
    get_events,
    get_recall,
    get_similar,
    get_stats,
    get_topics,
    get_trend,
    list_recalls,
    run_fda_ingest,
    run_fsis_ingest,
    run_ncc_ingest,
    run_seed_ingest,
    run_uk_ingest,
    search_companies,
)

router = APIRouter()

_RATE_LIMITED: dict[int | str, dict[str, Any]] = {
    429: {"description": "Rate limit exceeded — 60 requests/min per IP."}
}


def _validate_date_range(since: date | None, until: date | None) -> None:
    # An inverted window silently returns zero rows; a 422 makes the bad input explicit instead.
    if since and until and since > until:
        raise HTTPException(status_code=422, detail="`since` must be on or before `until`.")


@router.get(
    "",
    response_model=RecallListResult,
    summary="List recalls",
    description="Most-recent-first, paginated. All filters are optional and combine (AND).",
    responses=_RATE_LIMITED,
)
def get_recalls(
    response: Response,
    session: Session = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200, description="Max results to return (1–200)."),
    offset: int = Query(default=0, ge=0, description="Number of results to skip (pagination)."),
    country: RecallCountry | None = Query(
        default=None, description="Filter by country: us, uk, or za."
    ),
    category: RecallCategory | None = Query(default=None, description="Filter by cause category."),
    classification: RecallClass | None = Query(
        default=None, description="Filter by recall classification / alert type."
    ),
    source: RecallSource | None = Query(
        default=None, description="Filter by data source, e.g. fda, usda, uk, ncc."
    ),
    state: str | None = Query(
        default=None,
        max_length=50,
        description="Affected state — matches any recall touching this 2-letter code (e.g. CA).",
    ),
    company: str | None = Query(
        default=None,
        max_length=100,
        description="Filter by company name (case-insensitive partial match).",
    ),
    entity: str | None = Query(
        default=None,
        max_length=100,
        description=(
            "Filter to recalls naming this allergen/pathogen/hazard/contaminant — exact "
            "canonical value, e.g. Listeria or peanuts (the values returned in byEntity)."
        ),
    ),
    min_severity: float | None = Query(
        default=None,
        alias="minSeverity",
        ge=0,
        le=100,
        description="Only recalls at or above this severity score (0–100).",
    ),
    since: date | None = Query(
        default=None, description="Only recalls reported on or after this date (YYYY-MM-DD)."
    ),
    until: date | None = Query(
        default=None, description="Only recalls reported on or before this date (YYYY-MM-DD)."
    ),
    search: str | None = Query(
        default=None,
        max_length=200,
        description="Full-text search across product, reason, and company name.",
    ),
    severity: SeverityLabel | None = Query(
        default=None,
        description="Filter to a severity band: low, moderate, high, or severe.",
    ),
    topic: str | None = Query(
        default=None, description="Filter to a theme — a slug from /recalls/topics."
    ),
    event: str | None = Query(
        default=None, description="Filter to an event/outbreak — a slug from /recalls/events."
    ),
    sort: RecallSort = Query(
        default=RecallSort.recency,
        description="Order: recency (newest first, the default) or severity (most severe first).",
    ),
) -> RecallListResult:
    _validate_date_range(since, until)
    # Public read — daily-updated data, so let browsers/CDN cache it briefly.
    response.headers["Cache-Control"] = "public, max-age=120"
    return list_recalls(
        session,
        limit=limit,
        offset=offset,
        country=country.value if country else None,
        source=source.value if source else None,
        category=category.value if category else None,
        classification=classification.value if classification else None,
        state=state,
        company=company,
        entity=entity,
        min_severity=min_severity,
        severity=severity.value if severity else None,
        topic=topic,
        event=event,
        since=since,
        until=until,
        search=search,
        sort=sort.value,
    )


@router.get(
    "/stats",
    response_model=RecallStats,
    summary="Aggregate stats",
    description=(
        "Totals, counts by category, month, classification, state, company, source, and entity, "
        "plus anomaly callouts, a short-horizon volume forecast, and the last successful ingest "
        "time."
    ),
    responses=_RATE_LIMITED,
)
def recall_stats(
    response: Response,
    session: Session = Depends(get_session),
    country: RecallCountry | None = Query(default=None, description="Scope stats to a country."),
) -> RecallStats:
    response.headers["Cache-Control"] = "public, max-age=300"
    return get_stats(session, country.value if country else None)


@router.get(
    "/trend",
    response_model=TrendResult,
    summary="Monthly trend",
    description="Monthly recall counts, optionally grouped by cause category or data source.",
    responses=_RATE_LIMITED,
)
def recall_trend(
    response: Response,
    session: Session = Depends(get_session),
    country: RecallCountry | None = Query(default=None, description="Scope to a country."),
    group: TrendGroup = Query(
        default=TrendGroup.total,
        description="Group by: total, category, source, severity, or classification.",
    ),
    category: RecallCategory | None = Query(default=None, description="Filter by cause category."),
    classification: RecallClass | None = Query(
        default=None, description="Filter by recall classification / alert type."
    ),
    source: RecallSource | None = Query(
        default=None, description="Filter by data source, e.g. fda, usda, uk, ncc."
    ),
    state: str | None = Query(
        default=None,
        max_length=50,
        description="Affected state — matches any recall touching this 2-letter code (e.g. CA).",
    ),
    company: str | None = Query(
        default=None,
        max_length=100,
        description="Filter by company name (case-insensitive partial match).",
    ),
    entity: str | None = Query(
        default=None,
        max_length=100,
        description=(
            "Filter to recalls naming this allergen/pathogen/hazard/contaminant — exact "
            "canonical value, e.g. Listeria or peanuts (the values returned in byEntity)."
        ),
    ),
    min_severity: float | None = Query(
        default=None,
        alias="minSeverity",
        ge=0,
        le=100,
        description="Only recalls at or above this severity score (0–100).",
    ),
    since: date | None = Query(
        default=None, description="Only recalls reported on or after this date (YYYY-MM-DD)."
    ),
    until: date | None = Query(
        default=None, description="Only recalls reported on or before this date (YYYY-MM-DD)."
    ),
    search: str | None = Query(
        default=None,
        max_length=200,
        description="Full-text search across product, reason, and company name.",
    ),
    severity: SeverityLabel | None = Query(
        default=None,
        description="Filter to a severity band: low, moderate, high, or severe.",
    ),
    topic: str | None = Query(
        default=None, description="Filter to a theme — a slug from /recalls/topics."
    ),
    event: str | None = Query(
        default=None, description="Filter to an event/outbreak — a slug from /recalls/events."
    ),
) -> TrendResult:
    _validate_date_range(since, until)
    response.headers["Cache-Control"] = "public, max-age=300"
    return get_trend(
        session,
        country.value if country else None,
        group.value,
        category=category.value if category else None,
        classification=classification.value if classification else None,
        state=state,
        company=company,
        source=source.value if source else None,
        entity=entity,
        min_severity=min_severity,
        severity=severity.value if severity else None,
        topic=topic,
        event=event,
        since=since,
        until=until,
        search=search,
    )


@router.get(
    "/companies",
    response_model=list[str],
    summary="Company name suggestions",
    description=(
        "Distinct company names matching `q`, ranked by recall count — feeds the company "
        "filter's type-ahead."
    ),
    responses=_RATE_LIMITED,
)
def recall_companies(
    response: Response,
    session: Session = Depends(get_session),
    country: RecallCountry | None = Query(default=None, description="Scope to a country."),
    q: str = Query(
        default="", max_length=100, description="Search term (case-insensitive substring)."
    ),
) -> list[str]:
    response.headers["Cache-Control"] = "public, max-age=300"
    return search_companies(session, country.value if country else None, q)


@router.get(
    "/topics",
    response_model=list[TopicOut],
    summary="Recall themes",
    description=(
        "Themes discovered across recalls (NMF over the reason/product text), largest first. "
        "Scope the list or trend to one with `topic=<slug>`."
    ),
    responses=_RATE_LIMITED,
)
def recall_topics(
    response: Response,
    session: Session = Depends(get_session),
    country: RecallCountry | None = Query(default=None, description="Scope themes to a country."),
) -> list[TopicOut]:
    response.headers["Cache-Control"] = "public, max-age=300"
    return get_topics(session, country.value if country else None)


@router.get(
    "/events",
    response_model=list[EventOut],
    summary="Recall events & outbreaks",
    description=(
        "Recall clusters — recalls grouped into one incident (a shared pathogen within a time "
        "window, or the same FDA event). Outbreaks (multi-recall, pathogen-driven) come first. "
        "Scope the list or trend to one with `event=<slug>`."
    ),
    responses=_RATE_LIMITED,
)
def recall_events(
    response: Response,
    session: Session = Depends(get_session),
    country: RecallCountry | None = Query(default=None, description="Scope events to a country."),
    outbreaks_only: bool = Query(
        default=False, alias="outbreaksOnly", description="Return only the high-signal outbreaks."
    ),
) -> list[EventOut]:
    response.headers["Cache-Control"] = "public, max-age=300"
    return get_events(session, country.value if country else None, outbreaks_only=outbreaks_only)


@router.get(
    "/{source}/{recall_number}",
    response_model=RecallOut,
    summary="Get one recall",
    description="A single recall by its source + identifier - backs the recall detail page.",
    responses={**_RATE_LIMITED, 404: {"description": "No recall with that source + number."}},
)
def recall_detail(
    source: RecallSource,
    response: Response,
    recall_number: str = Path(
        max_length=256,
        description="The recall's identifier within its source (a number, or an NCC slug for ZA).",
    ),
    session: Session = Depends(get_session),
) -> RecallOut:
    response.headers["Cache-Control"] = "public, max-age=300"
    recall = get_recall(session, source.value, recall_number)
    if recall is None:
        raise HTTPException(status_code=404, detail="Recall not found.")
    return recall


@router.get(
    "/{source}/{recall_number}/similar",
    response_model=list[SimilarRecall],
    summary="Similar recalls",
    description=(
        "Recalls most similar to this one by reason/product text — precomputed cosine nearest "
        "neighbours over the shared TF-IDF matrix."
    ),
    responses=_RATE_LIMITED,
)
def recall_similar(
    source: RecallSource,
    response: Response,
    recall_number: str = Path(
        max_length=256,
        description="The recall's identifier within its source (a number, or an NCC slug for ZA).",
    ),
    session: Session = Depends(get_session),
    limit: int = Query(default=6, ge=1, le=20, description="Max similar recalls to return (1–20)."),
) -> list[SimilarRecall]:
    response.headers["Cache-Control"] = "public, max-age=300"
    return get_similar(session, source.value, recall_number, limit)


@router.post(
    "/ingest/fda",
    response_model=IngestResult,
    summary="Trigger an openFDA ingest",
    description="Fetches the latest recalls from openFDA and upserts them. Bearer-protected.",
    dependencies=[Depends(require_bearer)],
    responses={**_RATE_LIMITED, 401: {"description": "Missing or invalid bearer token."}},
)
def ingest(session: Session = Depends(get_session)) -> IngestResult:
    return run_fda_ingest(session)


@router.post(
    "/ingest/fsis",
    response_model=IngestResult,
    summary="Trigger a USDA FSIS ingest",
    description=(
        "Fetches the latest recalls + public health alerts from USDA FSIS and upserts them. "
        "Bearer-protected."
    ),
    dependencies=[Depends(require_bearer)],
    responses={**_RATE_LIMITED, 401: {"description": "Missing or invalid bearer token."}},
)
def ingest_fsis(session: Session = Depends(get_session)) -> IngestResult:
    return run_fsis_ingest(session)


@router.post(
    "/ingest/uk",
    response_model=IngestResult,
    summary="Trigger a UK FSA ingest",
    description=(
        "Fetches the latest food alerts from the UK FSA and upserts them. Bearer-protected."
    ),
    dependencies=[Depends(require_bearer)],
    responses={**_RATE_LIMITED, 401: {"description": "Missing or invalid bearer token."}},
)
def ingest_uk(session: Session = Depends(get_session)) -> IngestResult:
    return run_uk_ingest(session)


@router.post(
    "/ingest/ncc",
    response_model=IngestResult,
    summary="Trigger a South Africa NCC ingest",
    description=(
        "Crawls the National Consumer Commission's recall notices (WordPress REST API), keeps the "
        "human-food recalls, and upserts them. Bearer-protected."
    ),
    dependencies=[Depends(require_bearer)],
    responses={**_RATE_LIMITED, 401: {"description": "Missing or invalid bearer token."}},
)
def ingest_ncc(session: Session = Depends(get_session)) -> IngestResult:
    return run_ncc_ingest(session)


@router.post(
    "/ingest/seed",
    response_model=IngestResult,
    summary="Upsert the curated South Africa seed recalls",
    description=(
        "Upserts a small hand-maintained set of SA food recalls the NCC feed doesn't carry "
        "(Woolworths / Shoprite / NRCS). Bearer-protected."
    ),
    dependencies=[Depends(require_bearer)],
    responses={**_RATE_LIMITED, 401: {"description": "Missing or invalid bearer token."}},
)
def ingest_seed(session: Session = Depends(get_session)) -> IngestResult:
    return run_seed_ingest(session)
