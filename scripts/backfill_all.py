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
  stats payload (recall_stats)   the materialized /recalls/stats per country — recall aggregates +
                                 anomalies + forecast (all derived) -> scripts.build_stats

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
    build_stats,
)


class _Backfill(Protocol):
    # The contract every backfill module satisfies: a display name, a status check that reports
    # whether it still has work (and why), and a main() that runs it. Modules match structurally.
    NAME: str

    def status(self, session: Session) -> tuple[bool, str]: ...

    def main(self) -> None: ...


# Each backfill module owns its own "do I still need to run?" logic (its `status`), so adding a
# backfill is just a new scripts/backfill_*.py with NAME + status() + main(), then a line here.
# Order is dependency order — every edge in _TRIGGERS below points forward in this list, so a row
# never runs before something it depends on: entities before severity (severity is derived from
# entities), then build_analytics, build_events (reuses the neighbour graph build_analytics
# produces), and build_stats last (it materializes the payload from the now fully-derived recalls).
_BACKFILLS: list[_Backfill] = [
    backfill_fda,
    backfill_entities,
    backfill_severity,
    build_analytics,
    build_events,
    build_stats,
]


# Cross-backfill staleness: running one backfill changes data a later whole-corpus rebuild reads, so
# that rebuild goes stale the instant the first one runs — even if its own status() looked clean
# against the pre-run corpus. This maps each backfill to the others its run invalidates, so the plan
# printed up front already accounts for the knock-on work (the alternative — re-checking status()
# between runs — would be accurate but couldn't show the full plan before starting).
#
#   backfill_fda      seeds ~26k history rows. New rows self-populate entities/severity/category at
#                     ingest, so only the whole-corpus rebuilds need a rerun.
#   backfill_entities feeds severity (deadliest-pathogen bonus) and the stats entity aggregates.
#   backfill_severity feeds the stats severity aggregates.
#   build_analytics   rewrites topic_id (bumping updated_at, which stats keys off) and the neighbour
#                     graph build_events consumes.
#   build_events      rewrites event_cluster_id (bumps updated_at -> stats).
#
# Every edge points to a backfill later in _BACKFILLS, so one forward pass propagates the full set.
_TRIGGERS: dict[_Backfill, tuple[_Backfill, ...]] = {
    backfill_fda: (build_analytics, build_events, build_stats),
    backfill_entities: (backfill_severity, build_stats),
    backfill_severity: (build_stats,),
    build_analytics: (build_events, build_stats),
    build_events: (build_stats,),
}


def resolve_plan(
    status: dict[_Backfill, tuple[bool, str]], *, force_all: bool
) -> tuple[dict[_Backfill, bool], dict[_Backfill, str]]:
    """Expand each module's own status into the full run set: anything that will run drags in the
    backfills its run invalidates (_TRIGGERS), with a "triggered by X" reason. One forward pass over
    _BACKFILLS suffices because every trigger edge points forward in that list. Pure (no DB) so the
    planning logic stays unit-testable apart from the status() probes that feed it."""
    needed = {bf: force_all or status[bf][0] for bf in _BACKFILLS}
    reason = {bf: status[bf][1] for bf in _BACKFILLS}
    for bf in _BACKFILLS:
        if needed[bf]:
            for dep in _TRIGGERS.get(bf, ()):
                if not needed[dep]:
                    needed[dep] = True
                    reason[dep] = f"triggered by {bf.NAME}"
    return needed, reason


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
        status = {bf: bf.status(session) for bf in _BACKFILLS}
    finally:
        session.close()

    needed, reason = resolve_plan(status, force_all=args.all)

    for bf in _BACKFILLS:
        print(f"[{'RUN ' if needed[bf] else 'skip'}] {bf.NAME}: {reason[bf]}")

    if args.check:
        return

    ran = 0
    for bf in _BACKFILLS:
        if needed[bf]:
            print(f"\n=== {bf.NAME} ===")
            bf.main()
            ran += 1

    print(f"\nBackfill-all complete ({ran} of {len(_BACKFILLS)} run).")


if __name__ == "__main__":
    main()
