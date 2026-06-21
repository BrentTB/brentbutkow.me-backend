from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.recalls.models import Recall
from app.modules.recalls.severity import score_severity
from scripts._common import backfill_recalls

NAME = "severity"

# Recompute-dependency map (what to re-run when an input changes): scripts/backfill_all.py
_BATCH = 1000

# How many rows to re-score when deciding whether the backfill is still due (see status).
_PROBE_SAMPLE = 500


def status(session: Session) -> tuple[bool, str]:
    # Severity is derived from classification + category + entities + states + distribution_pattern,
    # so it goes stale when any of those change under it (e.g. an entities backfill adds the
    # deadliest-pathogen bonus). A score==0 check only catches never-scored rows, so instead probe:
    # re-score a sample and compare to what's stored. Real scores are never 0, so this still flags
    # rows left at the migration default. (Re-scoring is read-only here.)
    # Newest first so the probe is deterministic and weighted to recently-ingested rows.
    sample = session.scalars(
        select(Recall).order_by(Recall.report_date.desc()).limit(_PROBE_SAMPLE)
    ).all()
    stale = 0
    for recall in sample:
        score, label = score_severity(
            classification=recall.classification,
            category=recall.category,
            entities=recall.entities,
            states=recall.states,
            distribution_pattern=recall.distribution_pattern,
            reason_text=recall.reason_text,
        )
        if label != recall.severity_label or abs(score - recall.severity_score) > 0.05:
            stale += 1
    if stale:
        return True, f"{stale} of {len(sample)} sampled rows have a stale severity score"
    return False, f"{len(sample)} sampled rows checked; severity is current"


def _backfill(recall: Recall) -> None:
    score, label = score_severity(
        classification=recall.classification,
        category=recall.category,
        entities=recall.entities,
        states=recall.states,
        distribution_pattern=recall.distribution_pattern,
        reason_text=recall.reason_text,
    )
    recall.severity_score = score
    recall.severity_label = label


# One-time (re-runnable) pass: compute severity_score + severity_label for every stored recall from
# fields already on the row. New rows get severity at ingest via the normalizers — this seeds the
# existing rows the migration left at the 0 / 'low' server default.
# Run: `python -m scripts.backfill_severity`.
def main() -> None:
    backfill_recalls(_backfill, label="Backfilled severity for", batch=_BATCH)


if __name__ == "__main__":
    main()
