"""Unit tests for backfill_all's plan resolution — the _TRIGGERS cascade that drags downstream
rebuilds into the run set. Pure logic (no DB), so these run on the default database-free pytest."""

from scripts import backfill_all
from scripts.backfill_all import (
    backfill_entities,
    backfill_fda,
    backfill_severity,
    build_analytics,
    build_events,
    build_stats,
)


def _status(*needed):
    """Status dict over every backfill: the ones passed report work to do, the rest report clean."""
    flagged = set(needed)
    return {
        bf: (bf in flagged, "due" if bf in flagged else "clean") for bf in backfill_all._BACKFILLS
    }


def test_resolve_plan_runs_nothing_when_all_clean():
    needed, _ = backfill_all.resolve_plan(_status(), force_all=False)
    assert not any(needed.values())


def test_resolve_plan_fda_triggers_whole_corpus_rebuilds_only():
    # Seeding FDA history leaves the whole-corpus rebuilds stale, but new rows self-populate
    # entities/severity at ingest, so those in-place backfills stay skipped.
    needed, reason = backfill_all.resolve_plan(_status(backfill_fda), force_all=False)

    assert needed[backfill_fda]
    assert needed[build_analytics] and needed[build_events] and needed[build_stats]
    assert not needed[backfill_entities] and not needed[backfill_severity]
    assert reason[build_analytics] == "triggered by openFDA history seed"


def test_resolve_plan_entities_cascades_through_severity_to_stats():
    # entities -> severity -> stats is transitive: flagging only entities must pull in both.
    needed, reason = backfill_all.resolve_plan(_status(backfill_entities), force_all=False)

    assert needed[backfill_severity], "severity is derived from entities and must rerun"
    assert needed[build_stats]
    assert reason[backfill_severity] == "triggered by entities"
    # analytics/events don't depend on entities.
    assert not needed[build_analytics] and not needed[build_events]


def test_resolve_plan_keeps_original_reason_for_independently_due_backfill():
    # A backfill flagged by its own status keeps that reason; it isn't overwritten by a trigger.
    needed, reason = backfill_all.resolve_plan(_status(backfill_fda, build_stats), force_all=False)
    assert needed[build_stats]
    assert reason[build_stats] == "due"


def test_resolve_plan_force_all_runs_everything():
    needed, _ = backfill_all.resolve_plan(_status(), force_all=True)
    assert all(needed.values())
