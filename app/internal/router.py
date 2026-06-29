import hmac
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.subscriptions.dispatcher import run_dispatch

router = APIRouter()
# uvicorn's configured logger so these land alongside the access logs.
logger = logging.getLogger("uvicorn.error")

# Reject a second trigger while one is still running; a lock older than this is treated as stale (a
# crashed run) and reclaimed. In-process state suffices for the single-instance deploy — a
# multi-instance one would need a shared lock (DB row / Redis).
_LOCK_TTL = timedelta(minutes=10)
_lock_started_at: datetime | None = None


def _valid_token(provided: str) -> bool:
    expected = settings.internal_dispatch_token
    if not expected:
        # No secret configured → fail closed: reject every caller rather than run unauthenticated.
        return False
    # Encode before comparing: compare_digest raises on a non-ASCII header, which would surface as a
    # 500 instead of a clean 403. Encoding keeps the comparison constant-time and total.
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


@router.post(
    "/dispatch-alerts",
    summary="Trigger the daily alert dispatch",
    description=(
        "Guarded by the X-Internal-Token header. Called by the ingest job after a successful run. "
        "Idempotent within a 10-minute window — a still-running dispatch returns 409."
    ),
    responses={
        403: {"description": "Missing or invalid X-Internal-Token."},
        409: {"description": "A dispatch run is already in progress."},
    },
)
async def dispatch_alerts(
    x_internal_token: str = Header(default=""),
    session: Session = Depends(get_session),
) -> dict:
    global _lock_started_at  # noqa: PLW0603

    if not _valid_token(x_internal_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    now = datetime.now(UTC)
    if _lock_started_at is not None and now - _lock_started_at < _LOCK_TTL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A dispatch run is already in progress.",
        )

    _lock_started_at = now
    try:
        summary = await run_dispatch(session)
    finally:
        _lock_started_at = None

    return {"status": "ok", **summary}
