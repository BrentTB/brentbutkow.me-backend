from datetime import date

from fastapi import APIRouter, Depends, Query
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


@router.get("", response_model=RecallListResult)
def get_recalls(
    session: Session = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    category: RecallCategory | None = None,
    classification: RecallClass | None = None,
    since: date | None = None,
) -> RecallListResult:
    return list_recalls(
        session,
        limit=limit,
        offset=offset,
        category=category.value if category else None,
        classification=classification.value if classification else None,
        since=since,
    )


@router.get("/stats", response_model=RecallStats)
def recall_stats(session: Session = Depends(get_session)) -> RecallStats:
    return get_stats(session)


@router.post("/ingest", response_model=IngestResult, dependencies=[Depends(require_bearer)])
def ingest(session: Session = Depends(get_session)) -> IngestResult:
    return run_ingest(session)
