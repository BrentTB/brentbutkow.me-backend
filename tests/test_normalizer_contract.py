"""Shared contract every feed normalizer must satisfy.

Runs each feed's `normalize_*` over the same real-data fixtures its own test module maintains, and
asserts invariants that three production bugs violated — each of which needed a backfill script to
repair the corpus:

  * PR #28 — openFDA skipped `strip_html`, storing entities ("Reser&#039;s Fine Foods") raw while
    every other feed decoded them.
  * PR #16 — openFDA stored the recalling firm's state verbatim, leaking Canadian provinces and
    "N/A" into the US-state facet.
  * PR #27 — CFIA produced brand-less, meaningless product lines.

A future feed module that forgets a `normalize.py` helper (`strip_html`, `parse_us_state`) fails
here instead of in production. See the "External feeds" section of CLAUDE.md.
"""

import html
import re

import pytest

# Reuse the trimmed-from-real fixtures each per-feed test already maintains — don't invent data, so
# this contract tracks the shapes the real feeds actually send. These sibling modules are importable
# because pytest puts the tests directory on sys.path.
from test_cfia_ca import RECALL as CFIA_RECALL
from test_fsa_uk import ALERT as FSA_ALERT
from test_fsis import PHA as FSIS_PHA
from test_fsis import RECALL as FSIS_RECALL
from test_ncc_za import APTAMIL, BUTTANUTT, HUMMUS_MS, MCCAIN_STUB

from app.modules.recalls import cfia_ca, fsa_uk, fsis, ncc_za, openfda, seed_za
from app.modules.recalls.cfia_ca import CfiaRecord, normalize_cfia
from app.modules.recalls.fsa_uk import FsaRecord, normalize_fsa
from app.modules.recalls.fsis import FsisRecord, normalize_fsis
from app.modules.recalls.ncc_za import NccRecord, normalize_ncc
from app.modules.recalls.normalize import US_STATE_CODES, NormalizedRecall
from app.modules.recalls.openfda import OpenFdaRecord, normalize_recall
from app.modules.recalls.schemas import RecallCategory
from app.modules.recalls.seed_za import fetch_seed, normalize_seed

# Free-text fields that originate from feed payloads and must be plain text after normalization.
# Identifiers/enums (source, country, recall_number, source_url, status, classification) are left
# out — they aren't prose, and a URL legitimately carries characters an entity check would misread.
_TEXT_FIELDS = ("product_description", "reason_text", "company_name", "distribution_pattern")
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def _run(monkeypatch, module, normalizer, model, raws):
    # Isolate normalization from the ML classifier (matches each feed's own _normalize helper).
    monkeypatch.setattr(module, "classify", lambda _text: (RecallCategory.other, 0.5))
    return [normalizer(model.model_validate(raw)) for raw in raws]


def _openfda(monkeypatch):
    # openFDA builds records inline in its tests; mirror the two bug-relevant shapes verbatim — the
    # HTML-entity firm/product/reason (PR #28) and the non-US "state" (PR #16) — plus a clean row.
    monkeypatch.setattr(openfda, "classify", lambda _text: (RecallCategory.other, 0.0))
    records = [
        OpenFdaRecord(
            recall_number="H-1",
            recalling_firm="Reser&#039;s Fine Foods",
            product_description="Chicken &amp; Rice Bowl",
            reason_for_recall="May contain undeclared milk &amp; soy.",
            state="Ontario",
        ),
        OpenFdaRecord(
            recall_number="F-0276-2017",
            classification="Class II",
            product_description="CytoDetox",
            reason_for_recall="Product contains undeclared milk.",
            recalling_firm="Pharmatech LLC",
            state="FL",
        ),
    ]
    return [normalize_recall(r) for r in records]


def _seed_za(monkeypatch):
    # The curated seed list is the source itself — normalize every real entry (nothing to mirror).
    monkeypatch.setattr(seed_za, "classify", lambda _text: (RecallCategory.other, 0.5))
    return [normalize_seed(entry) for entry in fetch_seed()]


# feed id -> builder(monkeypatch) returning the list of normalized records to check.
_FEEDS = {
    "openfda": _openfda,
    "fsis": lambda mp: _run(mp, fsis, normalize_fsis, FsisRecord, [FSIS_RECALL, FSIS_PHA]),
    "fsa_uk": lambda mp: _run(mp, fsa_uk, normalize_fsa, FsaRecord, [FSA_ALERT]),
    "ncc_za": lambda mp: _run(
        mp, ncc_za, normalize_ncc, NccRecord, [BUTTANUTT, APTAMIL, MCCAIN_STUB, HUMMUS_MS]
    ),
    "cfia_ca": lambda mp: _run(mp, cfia_ca, normalize_cfia, CfiaRecord, [CFIA_RECALL]),
    "seed_za": _seed_za,
}


def _assert_text_is_plain(feed: str, record: NormalizedRecall) -> None:
    for field in _TEXT_FIELDS:
        value = record.get(field)
        if value is None:
            continue
        assert html.unescape(value) == value, (
            f"{feed}: {field} still contains HTML entities ({value!r}) — the normalizer skipped "
            f"strip_html (PR #28)"
        )
        assert not _TAG.search(value), f"{feed}: {field} still contains HTML tags ({value!r})"


def _assert_states_are_us_codes(feed: str, record: NormalizedRecall) -> None:
    state = record.get("state")
    assert state is None or state in US_STATE_CODES, (
        f"{feed}: state {state!r} is not a US code — parse_us_state was skipped (PR #16)"
    )
    for code in record.get("states") or []:
        assert code in US_STATE_CODES, (
            f"{feed}: states entry {code!r} is not a US code — parse_us_state was skipped (PR #16)"
        )


def _assert_identity_present(feed: str, record: NormalizedRecall) -> None:
    for field in ("source", "country", "recall_number", "product_description", "reason_text"):
        value = record.get(field)
        assert isinstance(value, str) and value.strip(), (
            f"{feed}: required field {field!r} is empty ({value!r})"
        )


@pytest.mark.parametrize("feed", sorted(_FEEDS))
def test_normalizer_output_satisfies_contract(feed, monkeypatch):
    records = _FEEDS[feed](monkeypatch)
    assert records, f"{feed}: no fixtures were exercised"
    for record in records:
        _assert_text_is_plain(feed, record)
        _assert_states_are_us_codes(feed, record)
        _assert_identity_present(feed, record)


def test_contract_checks_reject_dirty_records():
    # Guard the guard: a record violating each invariant must be caught, so a real regression can't
    # slip past a checker that silently passes everything.
    with pytest.raises(AssertionError):
        _assert_text_is_plain("fake", {"product_description": "Reser&#039;s", "reason_text": "x"})  # type: ignore[typeddict-item]
    with pytest.raises(AssertionError):
        _assert_states_are_us_codes("fake", {"state": "Ontario", "states": None})  # type: ignore[typeddict-item]
    with pytest.raises(AssertionError):
        _assert_identity_present("fake", {"source": "x", "country": "x", "recall_number": "  "})  # type: ignore[typeddict-item]
