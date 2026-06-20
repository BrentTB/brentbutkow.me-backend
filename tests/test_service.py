"""Integration tests for the service layer against a real Postgres.

The service uses Postgres-only features (JSONB, `INSERT ... ON CONFLICT`, `to_char`), so these run
against a live database rather than SQLite. They are skipped unless TEST_DATABASE_URL points at a
reachable Postgres, keeping the default `pytest` run database-free.

    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/test pytest
"""

import os
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.modules.recalls import service
from app.modules.recalls.fsa_uk import FsaBusiness, FsaProblem, FsaProduct, FsaRecord, FsaStatus
from app.modules.recalls.fsis import FsisRecord
from app.modules.recalls.models import Recall
from app.modules.recalls.openfda import OpenFdaRecord
from app.modules.recalls.schemas import (
    RecallCategory,
    RecallClass,
    RecallCountry,
    RecallSource,
)

TEST_DB = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL (Postgres) to run service integration tests"
)


def _psycopg_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@pytest.fixture
def session():
    engine = create_engine(_psycopg_url(TEST_DB))
    try:
        engine.connect().close()
    except OperationalError as exc:  # pragma: no cover - depends on local env
        pytest.skip(f"cannot reach TEST_DATABASE_URL: {exc}")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s
    Base.metadata.drop_all(engine)
    engine.dispose()


def _record(number: str, **fields) -> OpenFdaRecord:
    return OpenFdaRecord(recall_number=number, **fields)


def _patch_fetch(monkeypatch, batch: list[OpenFdaRecord]) -> None:
    monkeypatch.setattr(service, "fetch_enforcement", lambda limit=1000: batch)


def _fsis_record(number: str, **fields) -> FsisRecord:
    return FsisRecord(field_recall_number=number, **fields)


def _patch_fsis(monkeypatch, batch: list[FsisRecord]) -> None:
    monkeypatch.setattr(service, "fetch_fsis", lambda: batch)


def _fsa_record(number: str, **fields) -> FsaRecord:
    return FsaRecord(notation=number, **fields)


def _patch_fsa(monkeypatch, batch: list[FsaRecord]) -> None:
    monkeypatch.setattr(service, "fetch_fsa", lambda: batch)


def _seed_multi_source(session, monkeypatch) -> None:
    # One row per source so country/source/state filters and the by_source/by_state aggregations
    # all have something distinct to match. D-2 (FSIS) is multi-state to exercise the jsonb unnest.
    _patch_fetch(
        monkeypatch,
        [_record("D-1", reason_for_recall="undeclared milk", state="CA", report_date="20240101")],
    )
    service.run_ingest(session)
    _patch_fsis(
        monkeypatch,
        [
            _fsis_record(
                "D-2",
                field_recall_reason=["listeria"],
                field_states=["California", "Texas"],
                field_recall_date="2024-02-01",
            )
        ],
    )
    service.run_fsis_ingest(session)
    _patch_fsa(
        monkeypatch,
        [
            _fsa_record(
                "D-3",
                title="alert",
                type=["https://data.food.gov.uk/food-alerts/def/PRIN"],
                created="2024-03-01",
            )
        ],
    )
    service.run_uk_ingest(session)


def test_run_ingest_dedupes_batch_and_upserts(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("R-1", reason_for_recall="undeclared milk", classification="Class I"),
            _record("R-2", reason_for_recall="listeria", classification="Class II"),
            # Duplicate PK within the batch — the keep-last dedupe must win, not error.
            _record("R-1", reason_for_recall="metal fragments", classification="Class III"),
        ],
    )

    result = service.run_ingest(session)

    assert result.fetched == 3
    assert result.upserted == 2
    rows = {r.recall_number: r for r in session.scalars(select(Recall)).all()}
    assert set(rows) == {"R-1", "R-2"}
    assert rows["R-1"].classification == "Class III"
    assert rows["R-1"].category == RecallCategory.foreign_material.value


def test_run_ingest_is_idempotent_on_conflict(session, monkeypatch):
    _patch_fetch(monkeypatch, [_record("R-1", reason_for_recall="undeclared milk")])
    service.run_ingest(session)

    _patch_fetch(monkeypatch, [_record("R-1", reason_for_recall="listeria")])
    service.run_ingest(session)

    rows = session.scalars(select(Recall)).all()
    assert len(rows) == 1
    assert rows[0].category == RecallCategory.pathogen.value


def test_list_recalls_filters_orders_and_paginates(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record(
                "A-1",
                reason_for_recall="undeclared milk",
                classification="Class I",
                report_date="20240101",
                state="CA",
                recalling_firm="Acme Foods",
            ),
            _record(
                "A-2",
                reason_for_recall="listeria",
                classification="Class II",
                report_date="20240301",
                state="NY",
                recalling_firm="Beta Bakery",
            ),
            _record(
                "A-3",
                reason_for_recall="metal",
                classification="Class I",
                report_date="20240201",
                state="CA",
                recalling_firm="Acme Foods",
            ),
        ],
    )
    service.run_ingest(session)

    newest_first = service.list_recalls(session, limit=50, offset=0)
    assert newest_first.total == 3
    assert [i.recall_number for i in newest_first.items] == ["A-2", "A-3", "A-1"]

    allergens = service.list_recalls(
        session, limit=50, offset=0, category=RecallCategory.allergen.value
    )
    assert [i.recall_number for i in allergens.items] == ["A-1"]

    class_i = service.list_recalls(session, limit=50, offset=0, classification="Class I")
    assert {i.recall_number for i in class_i.items} == {"A-1", "A-3"}

    in_ca = service.list_recalls(session, limit=50, offset=0, state="CA")
    assert {i.recall_number for i in in_ca.items} == {"A-1", "A-3"}

    # company is a case-insensitive partial match
    acme = service.list_recalls(session, limit=50, offset=0, company="acme")
    assert {i.recall_number for i in acme.items} == {"A-1", "A-3"}

    recent = service.list_recalls(session, limit=50, offset=0, since=date(2024, 2, 1))
    assert {i.recall_number for i in recent.items} == {"A-2", "A-3"}

    older = service.list_recalls(session, limit=50, offset=0, until=date(2024, 2, 1))
    assert {i.recall_number for i in older.items} == {"A-1", "A-3"}

    # A since+until window keeps only what falls inside it.
    windowed = service.list_recalls(
        session, limit=50, offset=0, since=date(2024, 2, 1), until=date(2024, 2, 28)
    )
    assert {i.recall_number for i in windowed.items} == {"A-3"}

    page_two = service.list_recalls(session, limit=1, offset=1)
    assert page_two.total == 3
    assert [i.recall_number for i in page_two.items] == ["A-3"]


def test_search_companies_ranks_by_count_and_matches_substring(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("C-1", recalling_firm="Acme Foods"),
            _record("C-2", recalling_firm="Acme Bakery"),
            _record("C-3", recalling_firm="Beta Foods"),
            _record("C-4", recalling_firm="Acme Foods"),
        ],
    )
    service.run_ingest(session)

    # Ranked by recall count: "Acme Foods" (2) leads the singletons.
    assert service.search_companies(session)[0] == "Acme Foods"
    # Case-insensitive substring match.
    assert set(service.search_companies(session, q="acme")) == {"Acme Foods", "Acme Bakery"}
    assert service.search_companies(session, q="beta") == ["Beta Foods"]


def test_list_recalls_full_text_search_ranks_and_is_injection_safe(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record(
                "F-1", product_description="frozen spinach", reason_for_recall="listeria found"
            ),
            _record("F-2", product_description="peanut butter", reason_for_recall="undeclared soy"),
            _record(
                "F-3",
                product_description="ice cream",
                reason_for_recall="possible listeria contamination",
                recalling_firm="Listeria Free Foods",
            ),
        ],
    )
    service.run_ingest(session)

    # @@ match: only the listeria records come back, and ts_rank puts F-3 (term in reason + company)
    # ahead of F-1 (single occurrence).
    listeria = service.list_recalls(session, limit=50, offset=0, search="listeria")
    assert [i.recall_number for i in listeria.items] == ["F-3", "F-1"]

    # websearch AND semantics across terms.
    soy = service.list_recalls(session, limit=50, offset=0, search="undeclared soy")
    assert {i.recall_number for i in soy.items} == {"F-2"}

    # tsquery metacharacters must be tolerated, not raise a raw Postgres error (the term is bound,
    # not interpolated). websearch_to_tsquery sanitizes them down to the searchable words.
    weird = service.list_recalls(session, limit=50, offset=0, search="listeria & | ! :*")
    assert {i.recall_number for i in weird.items} == {"F-1", "F-3"}

    # whitespace-only is normalized to "no search" → the filter is skipped, all rows return.
    assert service.list_recalls(session, limit=50, offset=0, search="   ").total == 3


def test_get_stats_aggregates_by_category_and_month(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("S-1", reason_for_recall="undeclared milk", report_date="20240101"),
            _record("S-2", reason_for_recall="undeclared soy", report_date="20240115"),
            _record("S-3", reason_for_recall="listeria", report_date="20240301"),
        ],
    )
    service.run_ingest(session)

    stats = service.get_stats(session)

    assert stats.total == 3
    by_category = {c.category: c.count for c in stats.by_category}
    assert by_category[RecallCategory.allergen.value] == 2
    assert by_category[RecallCategory.pathogen.value] == 1
    # Largest cause first, so the "By cause" breakdown leads with the biggest.
    assert [c.category for c in stats.by_category] == [
        RecallCategory.allergen.value,
        RecallCategory.pathogen.value,
    ]
    by_month = {m.month: m.count for m in stats.by_month}
    assert by_month["2024-01"] == 2
    assert by_month["2024-03"] == 1
    assert stats.last_ingest_at is not None
    # Too little history to baseline against → no false anomalies, but the field is present.
    assert stats.anomalies == []


def test_get_stats_by_entity_and_entity_filter(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("E-1", reason_for_recall="undeclared milk and soy", report_date="20240101"),
            _record("E-2", reason_for_recall="undeclared milk", report_date="20240115"),
            _record("E-3", reason_for_recall="possible listeria", report_date="20240301"),
        ],
    )
    service.run_ingest(session)

    # by_entity unnests the {type, value} array, so a recall naming several entities counts to each.
    by_entity = {(e.type.value, e.label): e.count for e in service.get_stats(session).by_entity}
    assert by_entity[("allergen", "milk")] == 2
    assert by_entity[("allergen", "soybeans")] == 1
    assert by_entity[("pathogen", "Listeria")] == 1

    # The entity filter matches any recall naming that canonical value (GIN @>).
    milk = service.list_recalls(session, limit=50, offset=0, entity="milk")
    assert {i.recall_number for i in milk.items} == {"E-1", "E-2"}
    e1 = next(i for i in milk.items if i.recall_number == "E-1")
    assert {(x.type.value, x.value) for x in e1.entities} == {
        ("allergen", "milk"),
        ("allergen", "soybeans"),
    }


def test_get_trend_groups_by_category_and_source(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("T-1", reason_for_recall="undeclared milk", report_date="20240101"),
            _record("T-2", reason_for_recall="listeria", report_date="20240115"),
            _record("T-3", reason_for_recall="undeclared soy", report_date="20240301"),
        ],
    )
    service.run_ingest(session)

    total = {b.month: b.count for b in service.get_trend(session, group="total").buckets}
    assert total["2024-01"] == 2 and total["2024-03"] == 1

    cat = {
        (b.month, b.group): b.count for b in service.get_trend(session, group="category").buckets
    }
    assert cat[("2024-01", "allergen")] == 1  # milk
    assert cat[("2024-01", "pathogen")] == 1  # listeria
    assert cat[("2024-03", "allergen")] == 1  # soy

    src = {(b.month, b.group): b.count for b in service.get_trend(session, group="source").buckets}
    assert src[("2024-01", "fda")] == 2


def test_get_trend_applies_the_recall_filters(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("T-1", reason_for_recall="undeclared milk", report_date="20240101"),
            _record("T-2", reason_for_recall="listeria", report_date="20240101"),
            _record("T-3", reason_for_recall="undeclared soy", report_date="20240301"),
        ],
    )
    service.run_ingest(session)

    # Unfiltered: Jan has 2 (milk + listeria), Mar has 1 (soy).
    total = {b.month: b.count for b in service.get_trend(session).buckets}
    assert total == {"2024-01": 2, "2024-03": 1}

    # category=allergen drops the listeria recall, so Jan falls to 1.
    allergen = {b.month: b.count for b in service.get_trend(session, category="allergen").buckets}
    assert allergen == {"2024-01": 1, "2024-03": 1}

    # entity narrows to a single allergen, and the filter still applies under grouping.
    milk = {b.month: b.count for b in service.get_trend(session, entity="milk").buckets}
    assert milk == {"2024-01": 1}
    milk_by_cat = service.get_trend(session, group="category", entity="milk").buckets
    assert [(b.month, b.group, b.count) for b in milk_by_cat] == [("2024-01", "allergen", 1)]


def test_run_fsis_ingest_maps_states_and_upserts(session, monkeypatch):
    _patch_fsis(
        monkeypatch,
        [
            _fsis_record(
                "F-1",
                field_recall_reason=["listeria"],
                field_recall_classification="Class I",
                field_states=["California", "New York"],
                field_establishment=["Acme Meats"],
                field_recall_url="https://www.fsis.usda.gov/recalls/F-1",
                field_recall_date="2024-02-01",
                field_active_notice="True",
            ),
        ],
    )

    result = service.run_fsis_ingest(session)

    assert result.fetched == 1
    assert result.upserted == 1
    row = session.scalars(select(Recall)).one()
    assert row.source == RecallSource.usda.value
    assert row.country == RecallCountry.us.value
    # Full names map to 2-letter codes; multi-state → no single primary `state`.
    assert row.states == ["CA", "NY"]
    assert row.state is None
    assert row.classification == "Class I"
    assert row.status == "Active"
    assert row.source_url == "https://www.fsis.usda.gov/recalls/F-1"
    assert row.category == RecallCategory.pathogen.value


def test_run_uk_ingest_classifies_and_upserts(session, monkeypatch):
    _patch_fsa(
        monkeypatch,
        [
            _fsa_record(
                "UK-1",
                title="Allergy alert",
                created="2024-03-01T09:00:00",
                type=["https://data.food.gov.uk/food-alerts/def/AA"],
                status=FsaStatus(label="Published"),
                alertURL="https://www.food.gov.uk/alert/UK-1",
                reportingBusiness=FsaBusiness(commonName="Beta Bakery"),
                problem=[FsaProblem(riskStatement="undeclared milk")],
                productDetails=[FsaProduct(productName="Choc Cake")],
            ),
        ],
    )

    result = service.run_uk_ingest(session)

    assert result.upserted == 1
    row = session.scalars(select(Recall)).one()
    assert row.source == RecallSource.uk.value
    assert row.country == RecallCountry.uk.value
    # AA alert-type URI → Allergy Alert; UK alerts carry no US state.
    assert row.classification == RecallClass.allergy_alert.value
    assert row.states is None
    assert row.company_name == "Beta Bakery"
    assert row.source_url == "https://www.food.gov.uk/alert/UK-1"
    assert row.category == RecallCategory.allergen.value


def test_list_recalls_filters_by_country_source_and_state(session, monkeypatch):
    _seed_multi_source(session, monkeypatch)

    us = service.list_recalls(session, limit=50, offset=0, country="us")
    assert {i.recall_number for i in us.items} == {"D-1", "D-2"}

    uk = service.list_recalls(session, limit=50, offset=0, country="uk")
    assert {i.recall_number for i in uk.items} == {"D-3"}

    usda = service.list_recalls(session, limit=50, offset=0, source="usda")
    assert {i.recall_number for i in usda.items} == {"D-2"}

    # The state filter matches via `states.contains`; D-2 (FSIS) affects CA + TX.
    in_tx = service.list_recalls(session, limit=50, offset=0, state="TX")
    assert {i.recall_number for i in in_tx.items} == {"D-2"}
    in_ca = service.list_recalls(session, limit=50, offset=0, state="CA")
    assert {i.recall_number for i in in_ca.items} == {"D-1", "D-2"}


def test_get_stats_by_source_state_and_country_scope(session, monkeypatch):
    _seed_multi_source(session, monkeypatch)

    stats = service.get_stats(session)
    assert stats.total == 3
    by_source = {s.label: s.count for s in stats.by_source}
    assert by_source == {"fda": 1, "usda": 1, "uk": 1}
    # The multi-state FSIS recall counts toward every state it touches (jsonb unnest); the UK row
    # has no `states` array and is excluded.
    by_state = {s.label: s.count for s in stats.by_state}
    assert by_state["CA"] == 2  # D-1 (FDA, CA) + D-2 (FSIS, CA+TX)
    assert by_state["TX"] == 1

    # country scoping restricts every aggregation to the chosen country.
    uk_stats = service.get_stats(session, country="uk")
    assert uk_stats.total == 1
    assert {s.label for s in uk_stats.by_source} == {"uk"}


def test_severity_scores_sort_filter_and_breakdown(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            # Class III allergen → low; Class I Listeria → severe; Class II mislabel → elevated.
            _record("V-1", reason_for_recall="undeclared milk", classification="Class III"),
            _record(
                "V-2",
                reason_for_recall="Listeria monocytogenes contamination",
                classification="Class I",
                state="CA",
            ),
            _record("V-3", reason_for_recall="incorrect label", classification="Class II"),
        ],
    )
    service.run_ingest(session)

    # The Class I Listeria recall is the most severe, so sort=severity surfaces it first.
    by_severity = service.list_recalls(session, limit=50, offset=0, sort="severity")
    assert by_severity.items[0].recall_number == "V-2"
    assert by_severity.items[0].severity_label == "severe"

    # min_severity floors the list to the high-severity recall(s).
    floored = service.list_recalls(session, limit=50, offset=0, min_severity=70)
    assert {i.recall_number for i in floored.items} == {"V-2"}

    # stats expose a worst-first by_severity breakdown that sums back to the corpus.
    stats = service.get_stats(session)
    counts = {s.label: s.count for s in stats.by_severity}
    assert counts["severe"] == 1
    assert sum(counts.values()) == 3
    assert [s.label for s in stats.by_severity] == ["severe", "elevated", "low"]
