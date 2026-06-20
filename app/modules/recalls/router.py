from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.auth import require_bearer
from app.db import get_session
from app.modules.recalls.schemas import (
    IngestResult,
    RecallCategory,
    RecallClass,
    RecallCountry,
    RecallListResult,
    RecallSort,
    RecallSource,
    RecallStats,
    TrendGroup,
    TrendResult,
)
from app.modules.recalls.service import (
    get_stats,
    get_trend,
    list_recalls,
    run_fsis_ingest,
    run_ingest,
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
    country: RecallCountry | None = Query(default=None, description="Filter by country: us or uk."),
    category: RecallCategory | None = Query(default=None, description="Filter by cause category."),
    classification: RecallClass | None = Query(
        default=None, description="Filter by recall classification / alert type."
    ),
    source: RecallSource | None = Query(
        default=None, description="Filter by data source: fda, usda, or uk."
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
        "plus anomaly callouts and the last successful ingest time."
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
        default=TrendGroup.total, description="Group by: total, category, or source."
    ),
    category: RecallCategory | None = Query(default=None, description="Filter by cause category."),
    classification: RecallClass | None = Query(
        default=None, description="Filter by recall classification / alert type."
    ),
    source: RecallSource | None = Query(
        default=None, description="Filter by data source: fda, usda, or uk."
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


@router.post(
    "/ingest/fda",
    response_model=IngestResult,
    summary="Trigger an openFDA ingest",
    description="Fetches the latest recalls from openFDA and upserts them. Bearer-protected.",
    dependencies=[Depends(require_bearer)],
    responses={**_RATE_LIMITED, 401: {"description": "Missing or invalid bearer token."}},
)
def ingest(session: Session = Depends(get_session)) -> IngestResult:
    return run_ingest(session)


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
