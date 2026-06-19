import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.modules.nullspace.models import Score
from app.modules.nullspace.schemas import ScoreOut, ScoreResult, ScoreSubmission
from app.modules.nullspace.service import create_score, evaluate_submission, list_scores
from app.rate_limit import client_ip, limiter

router = APIRouter()
# uvicorn's configured logger so these land alongside the access logs.
logger = logging.getLogger("uvicorn.error")

_RATE_LIMITED: dict[int | str, dict[str, Any]] = {429: {"description": "Rate limit exceeded."}}


@router.post(
    "/score",
    response_model=ScoreResult,
    summary="Submit a Null Space score",
    description=(
        "Public, rate-limited to 10/min per IP. The score is plausibility-checked server-side; "
        "implausible runs are accepted but hidden from the leaderboard."
    ),
    responses=_RATE_LIMITED,
)
@limiter.limit("10/minute")
def submit_score(
    request: Request,
    submission: ScoreSubmission,
    session: Session = Depends(get_session),
) -> ScoreResult:
    ip = client_ip(request)
    flagged, reason = evaluate_submission(submission)
    create_score(session, submission, ip_address=ip, flagged=flagged, flag_reason=reason)
    # Flagged runs return ok too — the client gets no signal it was caught (honeypot).
    if flagged:
        logger.info(
            "nullspace score flagged (%s) from ip=%s score=%d kills=%d wave=%d duration(s)=%d",
            reason,
            ip,
            submission.score,
            submission.kills,
            submission.wave,
            submission.duration_ms // 1000,
        )
    return ScoreResult(status="ok")


@router.get(
    "/leaderboard",
    response_model=list[ScoreOut],
    summary="Top Null Space scores",
    description="Public leaderboard, highest first. Optionally scoped to a game version.",
)
def get_leaderboard(
    version: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[Score]:
    return list_scores(session, version=version, limit=limit)
