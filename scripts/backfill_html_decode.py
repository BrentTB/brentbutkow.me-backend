from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.recalls.models import Recall
from app.modules.recalls.normalize import strip_html
from scripts._common import backfill_recalls

NAME = "html-decode"

_BATCH = 1000

# How many rows to re-decode when deciding whether the backfill is still due (status).
_PROBE_SAMPLE = 500


def _decode(recall: Recall) -> None:
    # Idempotent: strip_html on already-plain text is a no-op, so SQLAlchemy only issues an UPDATE
    # for rows that actually change. The persisted generated columns (search_vector, search_text)
    # recompute automatically on that UPDATE.
    recall.product_description = strip_html(recall.product_description)
    recall.reason_text = strip_html(recall.reason_text)
    recall.company_name = strip_html(recall.company_name) or None


def status(session: Session) -> tuple[bool, str]:
    # Probe: any row whose decoded text differs from what's stored still carries raw HTML entities
    # (e.g. FDA rows seeded before openfda.py decoded at ingest). Read-only re-decode of a sample.
    sample = session.scalars(
        select(Recall)
        .order_by(Recall.report_date.desc())  # deterministic, newest-first probe
        .limit(_PROBE_SAMPLE)
    ).all()
    stale = sum(
        1
        for r in sample
        if strip_html(r.product_description) != r.product_description
        or strip_html(r.reason_text) != r.reason_text
        or (strip_html(r.company_name) or None) != r.company_name
    )
    if stale:
        return True, f"{stale} of {len(sample)} sampled rows still carry raw HTML entities/tags"
    return False, f"{len(sample)} sampled rows checked; all already decoded"


# One-time (re-runnable) pass: decode HTML entities + strip tags in every stored recall's text
# fields. New rows are decoded at ingest by each source normalizer — this seeds the existing corpus
# (chiefly FDA rows written before openfda.py started calling strip_html).
def main() -> None:
    backfill_recalls(_decode, label="Decoded HTML in", batch=_BATCH)


if __name__ == "__main__":
    main()
