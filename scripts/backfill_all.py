"""Runs the data backfills, skipping any already done (each module reports its own status()).

Recompute-dependency map — what each derived field is built from, and how to re-derive it over the
existing corpus. New rows get all of this at ingest; these scripts re-stage it for older rows.

  entities                       from reason_text + the gazetteer (entities.py)
                                 -> scripts.backfill_entities
  category (+ confidence)        from reason_text + the model (classifier.joblib)
                                 -> scripts.reclassify
  severity (score + label)       from classification + category + entities + states +
                                 distribution_pattern + the rules in severity.py
                                 -> scripts.backfill_severity
  topics / topic_id / neighbors  from reason_text + product_description + analytics params
                                 -> scripts.build_analytics
  events / event_cluster_id      from the neighbour graph + shared pathogens within a time window
                                 -> scripts.build_events   (runs after build_analytics)

So when you change ...           re-run ...
  the reason_text mapping        reclassify (entities+category+severity), then build_analytics
  the product_description text   build_analytics
  the entity gazetteer           backfill_entities, then backfill_severity
  the classifier model           reclassify
  the severity rules             backfill_severity
  the analytics params           build_analytics, then build_events
  the clustering params          build_events

Severity sits downstream of entities and category, so changing either re-stages severity too;
reclassify recomputes those three together, build_analytics is always separate, and backfill_all
auto-detects every case above through each module's status().
"""

import argparse
from typing import Protocol

from sqlalchemy.orm import Session

from app.db import SessionLocal
from scripts import (
    backfill_entities,
    backfill_fda,
    backfill_severity,
    build_analytics,
    build_events,
)


class _Backfill(Protocol):
    # The contract every backfill module satisfies: a display name, a status check that reports
    # whether it still has work (and why), and a main() that runs it. Modules match structurally.
    NAME: str

    def status(self, session: Session) -> tuple[bool, str]: ...

    def main(self) -> None: ...


# Each backfill module owns its own "do I still need to run?" logic (its `status`), so adding a
# backfill is just a new scripts/backfill_*.py with NAME + status() + main(), then a line here.
# build_analytics then build_events run last — whole-corpus rebuilds that want the rows seeded
# first, and build_events reuses the neighbour graph build_analytics produces.
_BACKFILLS: list[_Backfill] = [
    backfill_fda,
    backfill_severity,
    backfill_entities,
    build_analytics,
    build_events,
]


# Runs the data backfills, skipping any whose own status reports it's already done. `--all` forces
# every one; `--check` prints the plan and exits without running anything.
def main() -> None:
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument(
        "--all", action="store_true", help="run every backfill regardless of its status"
    )
    parser.add_argument(
        "--check", action="store_true", help="print the plan and exit without running anything"
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        plan = [(bf, *bf.status(session)) for bf in _BACKFILLS]
    finally:
        session.close()

    for bf, needed, reason in plan:
        print(f"[{'RUN ' if args.all or needed else 'skip'}] {bf.NAME}: {reason}")

    if args.check:
        return

    ran = 0
    for bf, needed, _reason in plan:
        if args.all or needed:
            print(f"\n=== {bf.NAME} ===")
            bf.main()
            ran += 1

    print(f"\nBackfill-all complete ({ran} of {len(plan)} run).")


if __name__ == "__main__":
    main()
