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
from app.modules.recalls.models import Recall
from app.modules.recalls.openfda import OpenFdaRecord
from app.modules.recalls.schemas import RecallCategory

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
            ),
            _record(
                "A-2",
                reason_for_recall="listeria",
                classification="Class II",
                report_date="20240301",
            ),
            _record(
                "A-3", reason_for_recall="metal", classification="Class I", report_date="20240201"
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

    recent = service.list_recalls(session, limit=50, offset=0, since=date(2024, 2, 1))
    assert {i.recall_number for i in recent.items} == {"A-2", "A-3"}

    page_two = service.list_recalls(session, limit=1, offset=1)
    assert page_two.total == 3
    assert [i.recall_number for i in page_two.items] == ["A-3"]


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
    by_month = {m.month: m.count for m in stats.by_month}
    assert by_month["2024-01"] == 2
    assert by_month["2024-03"] == 1
    assert stats.last_ingest_at is not None
