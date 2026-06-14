from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.auth import require_bearer
from app.db import get_session
from app.modules.recalls.schemas import (
    IngestResult,
    RecallCategory,
    RecallClass,
    RecallListResult,
    RecallStats,
)
from app.modules.recalls.service import get_stats, list_recalls, run_ingest

router = APIRouter()

_RATE_LIMITED: dict[int | str, dict[str, Any]] = {
    429: {"description": "Rate limit exceeded — 60 requests/min per IP."}
}


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
    category: RecallCategory | None = Query(default=None, description="Filter by cause category."),
    classification: RecallClass | None = Query(
        default=None, description="Filter by FDA recall classification."
    ),
    state: str | None = Query(
        default=None,
        description="Recalling firm's state — exact match on the 2-letter code (e.g. CA).",
    ),
    company: str | None = Query(
        default=None, description="Filter by company name (case-insensitive partial match)."
    ),
    since: date | None = Query(
        default=None, description="Only recalls reported on or after this date (YYYY-MM-DD)."
    ),
) -> RecallListResult:
    # Public read — daily-updated data, so let browsers/CDN cache it briefly.
    response.headers["Cache-Control"] = "public, max-age=120"
    return list_recalls(
        session,
        limit=limit,
        offset=offset,
        category=category.value if category else None,
        classification=classification.value if classification else None,
        state=state,
        company=company,
        since=since,
    )


@router.get(
    "/stats",
    response_model=RecallStats,
    summary="Aggregate stats",
    description=(
        "Totals, counts by category, month, classification, state, and company, "
        "plus the last successful ingest time."
    ),
    responses=_RATE_LIMITED,
)
def recall_stats(response: Response, session: Session = Depends(get_session)) -> RecallStats:
    response.headers["Cache-Control"] = "public, max-age=300"
    return get_stats(session)


@router.post(
    "/ingest",
    response_model=IngestResult,
    summary="Trigger an openFDA ingest",
    description="Fetches the latest recalls from openFDA and upserts them. Bearer-protected.",
    dependencies=[Depends(require_bearer)],
    responses={**_RATE_LIMITED, 401: {"description": "Missing or invalid bearer token."}},
)
def ingest(session: Session = Depends(get_session)) -> IngestResult:
    return run_ingest(session)
