"""One-subscription-per-email guarantees.

Postgres-only (the fix relies on a functional unique index on lower(email) and a per-email
pg_advisory_xact_lock — neither exists on SQLite). Set TEST_DATABASE_URL to run.
"""

import os

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.subscriptions import service
from app.subscriptions.models import Subscription
from app.subscriptions.schemas import SubscriptionCreate

TEST_DB = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set TEST_DATABASE_URL (Postgres) to run uniqueness integration tests"
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
    # Only the subscriptions table — isolated, and brings the functional unique index with it.
    Subscription.__table__.drop(engine, checkfirst=True)
    Subscription.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s
    Subscription.__table__.drop(engine, checkfirst=True)
    engine.dispose()


def _payload(email: str = "a@b.com") -> SubscriptionCreate:
    return SubscriptionCreate(email=email, countries=["us"])


def _count(session) -> int:
    return session.scalar(select(func.count()).select_from(Subscription)) or 0


def test_resubscribe_after_unsubscribe_reuses_row(session, monkeypatch):
    monkeypatch.setattr(service, "_try_send_optin", lambda **kwargs: None)
    service.create(_payload(), session)
    row = session.scalars(select(Subscription)).one()
    row.status = "unsubscribed"
    session.commit()

    service.create(_payload(), session)  # resubscribe
    rows = list(session.scalars(select(Subscription)).all())
    assert len(rows) == 1  # reused, not duplicated
    assert rows[0].status == "pending_confirmation"


def test_create_is_case_insensitive_single_row(session, monkeypatch):
    # Also exercises the real pg_advisory_xact_lock / hashtext SQL on the second call's lookup.
    monkeypatch.setattr(service, "_try_send_optin", lambda **kwargs: None)
    service.create(_payload("Person@Example.com"), session)
    service.create(_payload("person@example.com"), session)
    assert _count(session) == 1


def test_unique_index_blocks_manual_duplicate(session):
    session.add(Subscription(email="x@y.com", countries=["us"], management_token="t1"))
    session.commit()
    # Same address, different case + different management token → only the email index rejects it.
    session.add(Subscription(email="X@Y.com", countries=["us"], management_token="t2"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    assert _count(session) == 1
