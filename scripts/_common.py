"""Shared scaffold for the in-place recall backfills (backfill_entities, backfill_severity,
reclassify): load the corpus once, mutate each row, and persist with the right commit cadence."""

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import defer

from app.db import SessionLocal
from app.modules.recalls.models import Recall


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
