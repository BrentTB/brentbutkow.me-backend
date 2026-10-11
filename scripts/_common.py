"""Shared scaffold for the in-place recall backfills (backfill_entities, backfill_severity,
reclassify): load the corpus once, mutate each row, and persist with the right commit cadence."""

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.db import SessionLocal
from app.modules.recalls import rasff_eu
from app.modules.recalls.models import Recall
from app.modules.recalls.schemas import RecallSource

# (source, recall_number, reason_text) -> the text category/entities are derived from.
CauseText = Callable[[str, str, str], str]


def backfill_recalls(
    mutate: Callable[[Recall], None], *, label: str, batch: int | None = None
) -> None:
    """Load every stored recall, apply `mutate` to each in place, and commit.

    `batch=None` commits once at the end (all-or-nothing — a mid-run failure rolls the whole pass
    back); a positive `batch` commits every `batch` rows and prints progress, so a long pass
    releases its work incrementally. The heavy `raw` JSONB is deferred so the full-corpus load stays
    light: `.all()` keeps memory bounded at the current corpus, but past ~100k rows switch to
    yield_per streaming. `mutate` must not read `recall.raw` — that would trigger a per-row reload.
    SessionLocal's expire_on_commit=False keeps the loaded rows usable across batch commits.
    """
    session = SessionLocal()
    try:
        recalls = session.scalars(select(Recall).options(defer(Recall.raw))).all()
        for index, recall in enumerate(recalls, start=1):
            mutate(recall)
            if batch and index % batch == 0:
                session.commit()
                print(f"  {index}/{len(recalls)}…")
        session.commit()
        print(f"{label} {len(recalls)} recalls.")
    finally:
        session.close()


def cause_text_resolver(session: Session) -> CauseText:
    """Map a stored recall to the same classification text its normalizer used at ingest.

    Most sources classify `reason_text` alone. RASFF also reads the structured hazard names, which
    live only in `raw` — and `backfill_recalls` defers `raw`, so re-deriving from `reason_text`
    alone silently drops every category/entity that came from the hazard field. Pull just that one
    JSON key for the RASFF rows (a small dict, not the raw blobs) and rebuild via the ingest helper.
    """
    hazards: dict[str, str | None] = {
        number: hazard
        for number, hazard in session.execute(
            select(Recall.recall_number, Recall.raw["HAZARD_CATEGORY_NAME"].astext).where(
                Recall.source == RecallSource.rasff.value
            )
        )
    }

    def resolve(source: str, recall_number: str, reason_text: str) -> str:
        if source == RecallSource.rasff.value:
            return rasff_eu.cause_text(reason_text, hazards.get(recall_number))
        return reason_text

    return resolve


def load_cause_text() -> CauseText:
    """cause_text_resolver on a short-lived session of its own, for a script's main()."""
    session = SessionLocal()
    try:
        return cause_text_resolver(session)
    finally:
        session.close()
