"""Integration tests for the service layer against a real Postgres.

The service uses Postgres-only features (JSONB, `INSERT ... ON CONFLICT`, `to_char`), so these run
against a live database rather than SQLite. They are skipped unless TEST_DATABASE_URL points at a
reachable Postgres, keeping the default `pytest` run database-free.

    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/test pytest
"""

import os
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.modules.recalls import service
from app.modules.recalls.fsa_uk import FsaBusiness, FsaProblem, FsaProduct, FsaRecord, FsaStatus
from app.modules.recalls.fsis import FsisRecord
from app.modules.recalls.models import (
    Recall,
    RecallAnalyticsBuild,
    RecallStatsCache,
    RecallTopic,
)
from app.modules.recalls.openfda import OpenFdaRecord
from app.modules.recalls.schemas import (
    RecallCategory,
    RecallClass,
    RecallCountry,
    RecallSource,
)
from scripts import build_analytics, build_stats

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
    service.run_fda_ingest(session)
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


def test_run_fda_ingest_dedupes_batch_and_upserts(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("R-1", reason_for_recall="undeclared milk", classification="Class I"),
            _record("R-2", reason_for_recall="listeria", classification="Class II"),
            # Duplicate PK within the batch — the keep-last dedupe must win, not error.
            _record("R-1", reason_for_recall="metal fragments", classification="Class III"),
        ],
    )

    result = service.run_fda_ingest(session)

    assert result.fetched == 3
    assert result.new == 2  # fresh DB, so both surviving rows are new
    assert result.upserted == 2
    rows = {r.recall_number: r for r in session.scalars(select(Recall)).all()}
    assert set(rows) == {"R-1", "R-2"}
    assert rows["R-1"].classification == "Class III"
    assert rows["R-1"].category == RecallCategory.foreign_material.value


def test_run_fda_ingest_is_idempotent_on_conflict(session, monkeypatch):
    _patch_fetch(monkeypatch, [_record("R-1", reason_for_recall="undeclared milk")])
    first = service.run_fda_ingest(session)
    assert first.new == 1  # brand new

    _patch_fetch(monkeypatch, [_record("R-1", reason_for_recall="listeria")])
    second = service.run_fda_ingest(session)
    assert second.new == 0  # same identity re-seen → updated, not new

    rows = session.scalars(select(Recall)).all()
    assert len(rows) == 1
    assert rows[0].category == RecallCategory.pathogen.value


def test_get_recall_returns_one_or_none(session, monkeypatch):
    _patch_fetch(monkeypatch, [_record("R-1", reason_for_recall="undeclared milk")])
    service.run_fda_ingest(session)

    found = service.get_recall(session, "fda", "R-1")
    assert found is not None
    assert found.recall_number == "R-1"
    assert found.source.value == "fda"

    assert service.get_recall(session, "fda", "does-not-exist") is None


def test_run_fda_ingest_counts_new_across_chunk_boundary(session, monkeypatch):
    # Regression: the new-vs-stored existence lookup must be chunked like the upsert. A single
    # row-wise IN over a full-history backfill's ~26k keys overflows Postgres' max_stack_depth,
    # and the chunked replacement must still union correctly across slices — so use a batch that
    # spans several _UPSERT_CHUNK boundaries and overlaps the stored set only partway.
    chunk = service._UPSERT_CHUNK
    size = 2 * chunk + 100  # >2 chunks, so both the upsert and the lookup span multiple slices

    first_batch = [_record(f"N-{i}", reason_for_recall="undeclared milk") for i in range(size)]
    _patch_fetch(monkeypatch, first_batch)
    first = service.run_fda_ingest(session)
    assert first.fetched == size
    assert first.new == size  # fresh DB → every key is new
    assert first.upserted == size

    # Shift the window forward by one chunk: the lower part overlaps stored keys, the upper part is
    # genuinely new. Exactly `chunk` keys (N-{size}..) sit past the first batch, and the lookup over
    # all `size` keys must span multiple slices to find the overlap.
    second_batch = [
        _record(f"N-{i}", reason_for_recall="listeria") for i in range(chunk, chunk + size)
    ]
    _patch_fetch(monkeypatch, second_batch)
    second = service.run_fda_ingest(session)
    assert second.fetched == size
    assert second.new == chunk  # only keys beyond the first batch are unseen

    # Union of both windows: N-0 .. N-{2*chunk+size-1}.
    assert len(session.scalars(select(Recall)).all()) == chunk + size


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
    service.run_fda_ingest(session)

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
    service.run_fda_ingest(session)

    # Each suggestion carries its count, ranked by it — "Acme Foods" (2) leads the singletons.
    top = service.search_companies(session)[0]
    assert top.label == "Acme Foods"
    assert top.count == 2
    # Case-insensitive substring match on the name.
    assert {c.label for c in service.search_companies(session, q="acme")} == {
        "Acme Foods",
        "Acme Bakery",
    }
    assert [c.label for c in service.search_companies(session, q="beta")] == ["Beta Foods"]


def test_search_companies_counts_honor_other_filters(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("C-1", recalling_firm="Acme Foods", state="CA"),
            _record("C-2", recalling_firm="Acme Foods", state="TX"),
            _record("C-3", recalling_firm="Beta Foods", state="CA"),
        ],
    )
    service.run_fda_ingest(session)

    # Unfiltered: Acme (2) leads Beta (1).
    assert {c.label: c.count for c in service.search_companies(session)} == {
        "Acme Foods": 2,
        "Beta Foods": 1,
    }
    # Company is a facet — counts re-tally under the other active filters. Scoping to CA drops
    # Acme's Texas recall, so each firm now shows 1.
    assert {c.label: c.count for c in service.search_companies(session, state="CA")} == {
        "Acme Foods": 1,
        "Beta Foods": 1,
    }


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
    service.run_fda_ingest(session)

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
    service.run_fda_ingest(session)

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
    # Likewise too short to forecast: no projection rather than a bad line (still present, empty).
    assert stats.forecast == []


def test_get_stats_forecasts_overall_volume(session, monkeypatch):
    # ≥ 2 years of monthly history → the seasonal forecaster projects the next 3 months on read.
    records = []
    year, month = 2023, 1
    for i in range(28):  # 2023-01 .. 2025-04
        stamp = f"{year}{month:02d}01"
        for k in range(3):  # a few recalls a month so counts aren't trivially zero
            records.append(
                _record(f"F-{i}-{k}", reason_for_recall="undeclared milk", report_date=stamp)
            )
        month += 1
        if month > 12:
            year, month = year + 1, 1
    _patch_fetch(monkeypatch, records)
    service.run_fda_ingest(session)

    forecast = service.get_stats(session).forecast

    assert len(forecast) == 3
    # The horizon starts at the month after the last fully-present one (2025-03).
    assert forecast[0].month == "2025-04"
    for point in forecast:
        assert 0 <= point.lower <= point.predicted <= point.upper
    # A flat 3/month history projects flat at ~3.
    assert abs(forecast[0].predicted - 3.0) < 0.5


def test_rebuild_stats_materializes_a_row_per_country(session, monkeypatch):
    _patch_fetch(monkeypatch, [_record("US-1", reason_for_recall="undeclared milk")])
    service.run_fda_ingest(session)
    _patch_fsa(
        monkeypatch,
        [
            _fsa_record(
                "UK-1",
                title="alert",
                type=["https://data.food.gov.uk/food-alerts/def/PRIN"],
                created="2024-03-01",
            )
        ],
    )
    service.run_uk_ingest(session)

    summary = service.rebuild_stats(session)

    assert summary == {"countries": 2}
    cached = {row.country for row in session.scalars(select(RecallStatsCache)).all()}
    assert cached == {"us", "uk"}
    # The stored JSONB payload reconstructs into the same RecallStats shape get_stats returns.
    assert service.get_stats(session, "us").total == 1
    assert service.get_stats(session, "uk").total == 1
    # Reading the materialized row returns exactly what a live recompute would (clean round-trip +
    # deterministic ordering), so materialization never changes the API response.
    assert (
        service.get_stats(session, "us").model_dump()
        == service.compute_stats(session, "us").model_dump()
    )


def test_get_stats_serves_the_materialized_row_not_a_live_recompute(session, monkeypatch):
    _patch_fetch(monkeypatch, [_record("S-1", reason_for_recall="undeclared milk")])
    service.run_fda_ingest(session)
    service.rebuild_stats(session)

    # Add a recall *after* the build — the materialized row must not reflect it.
    _patch_fetch(monkeypatch, [_record("S-2", reason_for_recall="listeria")])
    service.run_fda_ingest(session)

    assert service.get_stats(session, "us").total == 1  # stale value, served from the cache row
    assert service.compute_stats(session, "us").total == 2  # live, reflects the new recall


def test_get_stats_falls_back_to_live_compute_without_a_row(session, monkeypatch):
    _patch_fetch(monkeypatch, [_record("F-1", reason_for_recall="undeclared milk")])
    service.run_fda_ingest(session)

    # No rebuild_stats yet → no cache row → get_stats computes live rather than erroring.
    assert service.get_stats(session, "us").total == 1


def test_get_stats_country_none_is_always_live(session, monkeypatch):
    _patch_fetch(monkeypatch, [_record("F-1", reason_for_recall="undeclared milk")])
    service.run_fda_ingest(session)
    service.rebuild_stats(session)

    _patch_fetch(monkeypatch, [_record("F-2", reason_for_recall="listeria")])
    service.run_fda_ingest(session)

    # us is cached (stale at 1), but the None/"all" scope is never materialized → always live.
    assert service.get_stats(session, "us").total == 1
    assert service.get_stats(session, None).total == 2


def test_build_stats_status_reports_staleness(session, monkeypatch):
    _patch_fetch(monkeypatch, [_record("F-1", reason_for_recall="undeclared milk")])
    service.run_fda_ingest(session)

    assert build_stats.status(session)[0] is True  # never materialized

    service.rebuild_stats(session)
    assert build_stats.status(session)[0] is False  # fresh

    # Any recall changing after the build marks the cache stale — not just an ingest, but a
    # standalone reclassify or backfill that rewrites a derived column. updated_at (onupdate) moves
    # past computed_at, which is the signal status() reads. Bump severity_score to a genuinely new
    # value so an UPDATE actually fires (re-setting a column to its current value is a no-op and
    # wouldn't move updated_at).
    recall = session.scalars(select(Recall)).first()
    assert recall is not None
    recall.severity_score += 1
    session.commit()
    assert build_stats.status(session)[0] is True


def test_build_analytics_status_ignores_preexisting_null_topics(session):
    session.add(
        RecallTopic(
            id=1,
            country="us",
            slug="listeria",
            label="listeria",
            top_terms=["listeria"],
            size=1,
        )
    )
    session.add(RecallAnalyticsBuild(built_at=datetime(2024, 1, 1, tzinfo=UTC)))
    built_at = datetime(2024, 1, 1, tzinfo=UTC)
    session.add(
        Recall(
            source="fda",
            country="us",
            recall_number="A-1",
            product_description="milk",
            reason_text="listeria",
            company_name="Acme",
            category="food",
            category_confidence=0.0,
            raw={},
            topic_id=1,
            updated_at=built_at,
        )
    )
    session.add(
        Recall(
            source="fda",
            country="us",
            recall_number="A-2",
            product_description="milk",
            reason_text="listeria",
            company_name="Acme",
            category="food",
            category_confidence=0.0,
            raw={},
            topic_id=None,
            updated_at=built_at - timedelta(days=1),
        )
    )
    session.commit()

    assert build_analytics.status(session)[0] is False


def test_get_stats_by_entity_and_entity_filter(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("E-1", reason_for_recall="undeclared milk and soy", report_date="20240101"),
            _record("E-2", reason_for_recall="undeclared milk", report_date="20240115"),
            _record("E-3", reason_for_recall="possible listeria", report_date="20240301"),
        ],
    )
    service.run_fda_ingest(session)

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


def test_get_trend_groups_by_dimension(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record(
                "T-1",
                reason_for_recall="undeclared milk",
                classification="Class I",
                report_date="20240101",
            ),
            _record(
                "T-2",
                reason_for_recall="listeria",
                classification="Class II",
                report_date="20240115",
            ),
            _record("T-3", reason_for_recall="undeclared soy", report_date="20240301"),
        ],
    )
    service.run_fda_ingest(session)

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

    # Severity bands: the Class I allergen + Class II listeria land severe; unclassified soy is
    # moderate (category-only base).
    sev = {
        (b.month, b.group): b.count for b in service.get_trend(session, group="severity").buckets
    }
    assert sev[("2024-01", "severe")] == 2
    assert sev[("2024-03", "moderate")] == 1

    # Classification — the nullable column is coalesced so unclassified soy gets its own segment.
    cls = {
        (b.month, b.group): b.count
        for b in service.get_trend(session, group="classification").buckets
    }
    assert cls[("2024-01", "Class I")] == 1
    assert cls[("2024-01", "Class II")] == 1
    assert cls[("2024-03", "Unclassified")] == 1


def test_get_trend_applies_the_recall_filters(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("T-1", reason_for_recall="undeclared milk", report_date="20240101"),
            _record("T-2", reason_for_recall="listeria", report_date="20240101"),
            _record("T-3", reason_for_recall="undeclared soy", report_date="20240301"),
        ],
    )
    service.run_fda_ingest(session)

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
    assert result.new == 1
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

    assert result.new == 1
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


def test_get_facets_counts_each_dimension_under_the_filter_set(session, monkeypatch):
    _seed_multi_source(session, monkeypatch)

    facets = service.get_facets(session)
    # Unfiltered, each facet equals the global breakdown.
    assert {s.label: s.count for s in facets.source} == {"fda": 1, "usda": 1, "uk": 1}
    assert {s.label: s.count for s in facets.state} == {"CA": 2, "TX": 1}

    # country scopes every facet (it isn't any facet's own dimension, so it always applies).
    us = service.get_facets(session, country="us")
    assert {s.label: s.count for s in us.source} == {"fda": 1, "usda": 1}  # uk source is uk-only
    assert {s.label: s.count for s in us.state} == {"CA": 2, "TX": 1}


def test_get_facets_excludes_a_facets_own_filter_but_applies_the_rest(session, monkeypatch):
    _seed_multi_source(session, monkeypatch)

    facets = service.get_facets(session, source="fda")
    # source is this facet's own dimension, so its list ignores the source filter — every source
    # still shows (with its full count), so the user can switch to one without it disappearing.
    assert {s.label: s.count for s in facets.source} == {"fda": 1, "usda": 1, "uk": 1}
    # Every *other* facet honors source=fda: only D-1 (FDA, CA) survives.
    assert {s.label: s.count for s in facets.state} == {"CA": 1}


def test_get_facets_includes_company_and_entity_breakdowns(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record(
                "F-1", recalling_firm="Acme Foods", reason_for_recall="undeclared milk", state="CA"
            ),
            _record("F-2", recalling_firm="Beta Foods", reason_for_recall="listeria", state="TX"),
        ],
    )
    service.run_fda_ingest(session)

    facets = service.get_facets(session)
    assert {c.label: c.count for c in facets.company} == {"Acme Foods": 1, "Beta Foods": 1}
    pairs = {(e.type.value, e.label) for e in facets.entity}
    assert ("allergen", "milk") in pairs
    assert ("pathogen", "Listeria") in pairs

    # Both are facets too — other filters apply (state=CA keeps only Acme / its milk entity), while
    # each facet still excludes its own dimension.
    ca = service.get_facets(session, state="CA")
    assert {c.label: c.count for c in ca.company} == {"Acme Foods": 1}
    assert {(e.type.value, e.label) for e in ca.entity} == {("allergen", "milk")}


def test_get_facets_counts_themes_and_outbreaks(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            _record("T-1", reason_for_recall="undeclared milk", state="CA"),
            _record("T-2", reason_for_recall="undeclared milk", state="TX"),
            _record("T-3", reason_for_recall="listeria", state="CA"),
        ],
    )
    service.run_fda_ingest(session)
    # Assign theme + outbreak ids directly — the NMF/clustering build that normally sets them is
    # exercised elsewhere; here we just need rows carrying ids to count.
    rows = {r.recall_number: r for r in session.scalars(select(Recall)).all()}
    rows["T-1"].topic_id, rows["T-1"].event_cluster_id = 1, 10
    rows["T-2"].topic_id, rows["T-2"].event_cluster_id = 1, 11
    rows["T-3"].topic_id, rows["T-3"].event_cluster_id = 2, 10
    session.commit()

    facets = service.get_facets(session)
    assert facets.topic_counts == {"1": 2, "2": 1}
    assert facets.event_counts == {"10": 2, "11": 1}

    # Other filters apply: scope to CA keeps T-1 (topic 1 / event 10) and T-3 (topic 2 / event 10).
    ca = service.get_facets(session, state="CA")
    assert ca.topic_counts == {"1": 1, "2": 1}
    assert ca.event_counts == {"10": 2}


def test_severity_scores_sort_filter_and_breakdown(session, monkeypatch):
    _patch_fetch(
        monkeypatch,
        [
            # Class III allergen → low; Class I Listeria → severe; Class II mislabel → moderate.
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
    service.run_fda_ingest(session)

    # The Class I Listeria recall is the most severe, so sort=severity surfaces it first.
    by_severity = service.list_recalls(session, limit=50, offset=0, sort="severity")
    assert by_severity.items[0].recall_number == "V-2"
    assert by_severity.items[0].severity_label == "severe"

    # min_severity floors the list to the high-severity recall(s).
    floored = service.list_recalls(session, limit=50, offset=0, min_severity=70)
    assert {i.recall_number for i in floored.items} == {"V-2"}

    # the exact-band filter returns only recalls in that band.
    severe = service.list_recalls(session, limit=50, offset=0, severity="severe")
    assert {i.recall_number for i in severe.items} == {"V-2"}
    moderate = service.list_recalls(session, limit=50, offset=0, severity="moderate")
    assert {i.recall_number for i in moderate.items} == {"V-3"}

    # stats expose a worst-first by_severity breakdown that sums back to the corpus.
    stats = service.get_stats(session)
    counts = {s.label: s.count for s in stats.by_severity}
    assert counts["severe"] == 1
    assert sum(counts.values()) == 3
    assert [s.label for s in stats.by_severity] == ["severe", "moderate", "low"]


def test_analytics_topics_neighbours_and_topic_filter(session, monkeypatch):
    from app.modules.recalls import analytics

    # Three pairs of near-duplicate recalls so every doc shares ≥2-document-frequency terms with its
    # partner — the default min_df keeps them, so every recall gets a topic + neighbours.
    _patch_fetch(
        monkeypatch,
        [
            _record(
                "N-1",
                reason_for_recall="Listeria monocytogenes in deli meat",
                product_description="sliced deli turkey",
            ),
            _record(
                "N-2",
                reason_for_recall="Listeria contamination in deli meat",
                product_description="deli turkey slices",
            ),
            _record(
                "N-3",
                reason_for_recall="undeclared peanuts in cookies",
                product_description="chocolate peanut cookies",
            ),
            _record(
                "N-4",
                reason_for_recall="undeclared peanuts in cookies",
                product_description="peanut cookies pack",
            ),
            _record(
                "N-5",
                reason_for_recall="metal fragments in frozen pizza",
                product_description="frozen pizza",
            ),
            _record(
                "N-6",
                reason_for_recall="metal fragments in frozen pizza",
                product_description="frozen pizza pack",
            ),
        ],
    )
    service.run_fda_ingest(session)

    summary = analytics.rebuild_analytics(session)
    assert summary["recalls"] == 6
    assert summary["topics"] >= 1
    assert summary["neighbors"] > 0

    # Topics are materialised with terms + sizes + a stable slug; every recall is assigned to one.
    topics = service.get_topics(session)
    assert topics
    assert all(topic.top_terms for topic in topics)
    assert all(topic.slug for topic in topics)  # a readable key was generated, not left blank
    assert sum(topic.size for topic in topics) == 6

    # The nearest neighbour of one deli-meat Listeria recall is its near-duplicate.
    similar = service.get_similar(session, "fda", "N-1", 3)
    assert similar
    assert similar[0].recall.recall_number == "N-2"
    assert 0 < similar[0].similarity <= 1

    # The topic filter narrows the list to a theme's members — keyed by the stable slug, which is
    # what a bookmark / shared URL carries (not the volatile surrogate id).
    recall = session.get(Recall, ("fda", "N-1"))
    assert recall.topic_id is not None
    topic_row = session.get(RecallTopic, recall.topic_id)
    members = service.list_recalls(session, limit=50, offset=0, topic=topic_row.slug)
    assert "N-1" in {item.recall_number for item in members.items}

    # The serialised recall carries its topicId (the field the frontend reads for the theme chip).
    n1 = next(item for item in members.items if item.recall_number == "N-1")
    assert n1.topic_id == recall.topic_id

    # Themes are per-country: the seeded FDA recalls have US themes, and the UK has none here.
    assert service.get_topics(session, country="us")
    assert service.get_topics(session, country="uk") == []


def test_events_clustering_filter_and_serialisation(session, monkeypatch):
    from app.modules.recalls import analytics, events

    # Three Listeria deli-meat recalls sharing an FDA event — one outbreak (≥3 + shared pathogen);
    # the peanut recall is unrelated and stays a singleton.
    _patch_fetch(
        monkeypatch,
        [
            _record(
                "E-1",
                reason_for_recall="Listeria monocytogenes in deli meat",
                product_description="sliced deli turkey",
                event_id="EV-1",
                report_date="20260101",
            ),
            _record(
                "E-2",
                reason_for_recall="Listeria contamination in deli meat",
                product_description="deli turkey slices",
                event_id="EV-1",
                report_date="20260115",
            ),
            _record(
                "E-3",
                reason_for_recall="Listeria found in sliced deli meat",
                product_description="turkey deli slices",
                event_id="EV-1",
                report_date="20260128",
            ),
            _record(
                "E-4",
                reason_for_recall="undeclared peanuts in cookies",
                product_description="peanut cookies",
                report_date="20260101",
            ),
        ],
    )
    service.run_fda_ingest(session)
    analytics.rebuild_analytics(session)  # events reuse the materialised neighbour graph
    summary = events.rebuild_events(session)
    assert summary["events"] >= 1
    assert summary["outbreaks"] >= 1

    outbreak = next(e for e in service.get_events(session) if e.is_outbreak)
    assert outbreak.dominant_entity == "Listeria"
    assert outbreak.recall_count == 3
    assert outbreak.slug  # a stable, readable key was generated
    assert service.get_events(session, outbreaks_only=True)  # the outbreaks-only scope returns it

    # The `event` filter narrows the list to the cluster's members by stable slug.
    members = service.list_recalls(session, limit=50, offset=0, event=outbreak.slug)
    assert {item.recall_number for item in members.items} == {"E-1", "E-2", "E-3"}

    # The serialised recall carries its eventClusterId (the field the frontend reads).
    e1 = next(item for item in members.items if item.recall_number == "E-1")
    assert e1.event_cluster_id is not None
