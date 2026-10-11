from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.recalls.entities import extract_entities
from app.modules.recalls.models import Recall
from scripts._common import CauseText, backfill_recalls, cause_text_resolver, load_cause_text

NAME = "entities"

# Recompute-dependency map (what to re-run when an input changes): scripts/backfill_all.py
_BATCH = 1000

# How many empty-entity rows to re-extract when deciding whether the backfill is still due (status).
_PROBE_SAMPLE = 500


def status(session: Session) -> tuple[bool, str]:
    # An empty entities array is ambiguous: the recall may genuinely name nothing, or extraction
    # never ran for it (rows seeded before the feature, or before a gazetteer change) — a plain
    # empty-count can't tell those apart. So probe: re-extract a sample of the empty rows; if any
    # would gain entities, those rows are stale and the backfill is due. (Read-only re-extraction.)
    cause_text = cause_text_resolver(session)
    sample = session.execute(
        select(Recall.source, Recall.recall_number, Recall.reason_text)
        .where(func.jsonb_array_length(Recall.entities) == 0)
        .order_by(Recall.report_date.desc())  # deterministic, newest-first probe
        .limit(_PROBE_SAMPLE)
    ).all()
    stale = sum(1 for row in sample if extract_entities(cause_text(*row)))
    if stale:
        return True, f"{stale} of {len(sample)} sampled empty rows would gain entities"
    return False, f"{len(sample)} sampled empty rows checked; none would gain entities"


def _backfill(recall: Recall, cause_text: CauseText) -> None:
    # The same text ingest extracted from (RASFF adds its hazard names), not reason_text alone.
    recall.entities = extract_entities(
        cause_text(recall.source, recall.recall_number, recall.reason_text)
    )


# One-time (re-runnable) pass: extract entities from every stored recall's reason_text. New rows
# get entities at ingest via the normalizers — this seeds the existing backfill.
def main() -> None:
    cause_text = load_cause_text()
    backfill_recalls(
        lambda recall: _backfill(recall, cause_text),
        label="Backfilled entities for",
        batch=_BATCH,
    )


if __name__ == "__main__":
    main()
