from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.modules.nullspace.models import Score
from app.modules.nullspace.schemas import FlagReason, ScoreSubmission

_LEADERBOARD_LIMIT = 50
# Cap flagged rows so the honeypot can't grow the table without bound.
_MAX_FLAGGED_ROWS = 500
# Cap the whole table: submissions are unauthenticated (rate-limited only), so prune
# the lowest scores first — a leaderboard keeps its best entries, not its newest.
_MAX_ROWS = 50_000

# Plausibility ceilings. Deliberately generous: the goal is to stop blatant
# fabrication (a console-posted 9,999,999), not to police borderline runs, so each
# favours a false negative over rejecting a legitimate high score. The game is
# client-side, so this can never be airtight — but every check runs server-side,
# where a tampered client can't reach it.
_MAX_SCORE_PER_KILL = 1_000  # even the toughest boss is worth well under this
_SCORE_BASE = 1_000  # slack so a low-kill early death never trips the score check
_MAX_KILLS_PER_WAVE = 80  # far above the enemies a single wave actually spawns
_MIN_MS_PER_WAVE = 10_000  # a wave can't realistically be cleared faster than this


def evaluate_submission(submission: ScoreSubmission) -> tuple[bool, FlagReason | None]:
    """Pure plausibility check — returns (flagged, reason). No DB access.

    Score is awarded per kill, so it must track the kill count; kills must track the
    wave reached; and reaching a wave takes time. A forged value violates one of these.
    """
    if submission.score > _SCORE_BASE + submission.kills * _MAX_SCORE_PER_KILL:
        return True, FlagReason.score_exceeds_kills
    if submission.kills > _MAX_KILLS_PER_WAVE * (submission.wave + 1):
        return True, FlagReason.kills_exceed_wave
    if submission.wave > 1 and submission.duration_ms < submission.wave * _MIN_MS_PER_WAVE:
        return True, FlagReason.too_fast_for_wave
    return False, None


def create_score(
    session: Session,
    submission: ScoreSubmission,
    *,
    ip_address: str | None,
    flagged: bool,
    flag_reason: FlagReason | None,
) -> Score:
    score = Score(
        name=submission.name,
        score=submission.score,
        kills=submission.kills,
        wave=submission.wave,
        level=submission.level,
        duration_ms=submission.duration_ms,
        ship_kind=submission.ship_kind,
        version=submission.version,
        currency=submission.currency,
        space_metal=submission.space_metal,
        upgrades_purchased=submission.upgrades_purchased,
        ultimates_owned=submission.ultimates_owned,
        ip_address=ip_address,
        flagged=flagged,
        flag_reason=flag_reason,
    )
    session.add(score)
    session.commit()
    session.refresh(score)
    if flagged:
        _prune_flagged(session)
    _prune_overall(session)
    return score


def _prune_flagged(session: Session) -> None:
    # Keep only the most recent _MAX_FLAGGED_ROWS flagged rows; delete the rest.
    keep = (
        select(Score.id)
        .where(Score.flagged.is_(True))
        .order_by(Score.created_at.desc(), Score.id.desc())
        .limit(_MAX_FLAGGED_ROWS)
    )
    session.execute(delete(Score).where(Score.flagged.is_(True), Score.id.not_in(keep)))
    session.commit()


def _prune_overall(session: Session) -> None:
    # Enforce the table cap by evicting the lowest scores first, so the leaderboard's
    # top entries survive (unlike contact messages, oldest-first would drop a long-
    # standing #1).
    over = (session.scalar(select(func.count()).select_from(Score)) or 0) - _MAX_ROWS
    if over <= 0:
        return
    ids = list(
        session.scalars(
            select(Score.id)
            .order_by(Score.score.asc(), Score.created_at.asc(), Score.id.asc())
            .limit(over)
        ).all()
    )
    if ids:
        session.execute(delete(Score).where(Score.id.in_(ids)))
    session.commit()


def list_scores(
    session: Session, *, version: str | None = None, limit: int = _LEADERBOARD_LIMIT
) -> list[Score]:
    # Highest score first; on a tie the earlier achiever ranks above. Flagged rows
    # are never served.
    query = select(Score).where(Score.flagged.is_(False))
    if version:
        query = query.where(Score.version == version)
    query = query.order_by(Score.score.desc(), Score.created_at.asc(), Score.id.asc()).limit(limit)
    return list(session.scalars(query).all())
