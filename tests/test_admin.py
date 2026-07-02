import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import issue_admin_token, verify_admin_token
from app.config import settings
from app.db import get_session
from app.main import app
from app.modules.admin import service as admin_service
from app.modules.admin.schemas import (
    AdminMessageUpdate,
    AdminOverview,
    AdminSubscriptionUpdate,
    IngestSummary,
    MessageCounts,
    NullspaceCounts,
    RecallCounts,
    SubscriptionCounts,
)
from app.modules.contact.models import Message
from app.rate_limit import limiter

app.dependency_overrides[get_session] = lambda: None
client = TestClient(app)

PASSWORD = "test-admin-password"  # matches tests/conftest.py


def _auth_header() -> dict[str, str]:
    res = client.post("/admin/login", json={"password": PASSWORD})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['token']}"}


# --- login -------------------------------------------------------------------


def test_login_success_returns_token_and_expiry():
    res = client.post("/admin/login", json={"password": PASSWORD})
    assert res.status_code == 200
    body = res.json()
    assert body["token"]
    assert body["expiresAt"]  # camelCase on the wire
    assert verify_admin_token(body["token"]) is True


def test_login_wrong_password_is_401():
    assert client.post("/admin/login", json={"password": "nope"}).status_code == 401


def test_login_blank_password_is_422():
    assert client.post("/admin/login", json={"password": ""}).status_code == 422


def test_login_fails_closed_when_password_unset(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", None)
    # Even the (otherwise) correct password is rejected when the server has no secret configured.
    assert client.post("/admin/login", json={"password": PASSWORD}).status_code == 401


# --- auth guard --------------------------------------------------------------


def test_overview_requires_token():
    assert client.get("/admin/overview").status_code == 401


def test_overview_rejects_non_bearer_scheme():
    token = client.post("/admin/login", json={"password": PASSWORD}).json()["token"]
    assert client.get("/admin/overview", headers={"Authorization": token}).status_code == 401


def test_tampered_token_is_rejected():
    headers = _auth_header()
    headers["Authorization"] += "x"  # corrupt the signature
    assert client.get("/admin/overview", headers=headers).status_code == 401


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "admin_session_ttl_seconds", -10)
    token, _ = issue_admin_token()
    res = client.get("/admin/overview", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401


# --- data endpoints (service layer stubbed; no DB) ---------------------------


def _fake_overview() -> AdminOverview:
    return AdminOverview(
        messages=MessageCounts(total=3, real=2, bot=1, unseen=1),
        subscriptions=SubscriptionCounts(
            total=5, active=4, pending_confirmation=1, paused=0, unsubscribed=0
        ),
        ingest=IngestSummary(
            last_run_at=datetime(2026, 6, 30, tzinfo=UTC),
            status="success",
            fetched_count=10,
            upserted_count=7,
        ),
        recalls=RecallCounts(total=100, us=80, uk=15, za=5, ca=0),
        nullspace=NullspaceCounts(total=42, legit=40, flagged=2),
    )


def test_overview_returns_summary(monkeypatch):
    monkeypatch.setattr(admin_service, "build_overview", lambda session: _fake_overview())
    res = client.get("/admin/overview", headers=_auth_header())
    assert res.status_code == 200
    body = res.json()
    assert body["messages"] == {"total": 3, "real": 2, "bot": 1, "unseen": 1}
    assert body["subscriptions"]["active"] == 4
    assert body["recalls"]["us"] == 80
    assert body["nullspace"] == {"total": 42, "legit": 40, "flagged": 2}


def test_messages_maps_rows_and_total(monkeypatch):
    row = SimpleNamespace(
        id=1,
        created_at=datetime(2026, 6, 30, tzinfo=UTC),
        message="hello",
        name="Ada",
        email="ada@example.com",
        timezone="UTC",
        locale="en",
        referrer=None,
        user_agent="curl",
        accept_language="en",
        ip_address="1.2.3.4",
        country="US",
        is_bot=False,
        bot_reason=None,
        seen=False,
    )
    monkeypatch.setattr(admin_service, "list_messages", lambda session, **kw: ([row], 1))
    res = client.get("/admin/messages", headers=_auth_header())
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["message"] == "hello"
    assert body["items"][0]["isBot"] is False
    assert body["items"][0]["seen"] is False


def test_messages_forwards_pagination_and_bot_flag(monkeypatch):
    captured: dict = {}

    def fake(session, **kw):
        captured.update(kw)
        return [], 0

    monkeypatch.setattr(admin_service, "list_messages", fake)
    res = client.get("/admin/messages?limit=10&offset=20&includeBots=true", headers=_auth_header())
    assert res.status_code == 200
    assert captured == {"limit": 10, "offset": 20, "include_bots": True, "seen": None}


def test_messages_forwards_seen_filter(monkeypatch):
    captured: dict = {}

    def fake(session, **kw):
        captured.update(kw)
        return [], 0

    monkeypatch.setattr(admin_service, "list_messages", fake)
    res = client.get("/admin/messages?seen=false", headers=_auth_header())
    assert res.status_code == 200
    assert captured["seen"] is False


def test_subscriptions_rejects_invalid_status(monkeypatch):
    monkeypatch.setattr(admin_service, "list_subscriptions", lambda session, **kw: ([], 0))
    res = client.get("/admin/subscriptions?status=bogus", headers=_auth_header())
    assert res.status_code == 422


def test_subscriptions_accepts_valid_status(monkeypatch):
    captured: dict = {}

    def fake(session, **kw):
        captured.update(kw)
        return [], 0

    monkeypatch.setattr(admin_service, "list_subscriptions", fake)
    res = client.get("/admin/subscriptions?status=active", headers=_auth_header())
    assert res.status_code == 200
    assert captured["status"] == "active"


# --- subscription edit -------------------------------------------------------

_SID = "11111111-1111-1111-1111-111111111111"


def _subscription_row(**overrides) -> SimpleNamespace:
    base = dict(
        id=uuid.UUID(_SID),
        email="a@b.com",
        status="paused",
        countries=["us"],
        entities=[],
        companies=[],
        categories=[],
        min_severity=None,
        confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 6, 30, tzinfo=UTC),
        last_digest_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeSession:
    def __init__(self, row):
        self.row = row
        self.committed = False

    def get(self, model, ident):
        return self.row

    def commit(self):
        self.committed = True

    def refresh(self, row):
        pass


def test_edit_subscription_returns_updated_row(monkeypatch):
    captured: dict = {}

    def fake(session, subscription_id, patch):
        captured["id"] = subscription_id
        captured["patch"] = patch
        return _subscription_row(status="unsubscribed")

    monkeypatch.setattr(admin_service, "update_subscription", fake)
    res = client.patch(
        f"/admin/subscriptions/{_SID}", json={"status": "unsubscribed"}, headers=_auth_header()
    )
    assert res.status_code == 200
    assert res.json()["status"] == "unsubscribed"
    assert str(captured["id"]) == _SID
    assert captured["patch"].status == "unsubscribed"


def test_edit_subscription_not_found_is_404(monkeypatch):
    monkeypatch.setattr(admin_service, "update_subscription", lambda *a, **k: None)
    res = client.patch(
        f"/admin/subscriptions/{_SID}", json={"status": "active"}, headers=_auth_header()
    )
    assert res.status_code == 404


def test_edit_subscription_rejects_lifecycle_status(monkeypatch):
    # pending_confirmation is part of the opt-in lifecycle, not an operator action → 422.
    monkeypatch.setattr(admin_service, "update_subscription", lambda *a, **k: None)
    res = client.patch(
        f"/admin/subscriptions/{_SID}",
        json={"status": "pending_confirmation"},
        headers=_auth_header(),
    )
    assert res.status_code == 422


def test_edit_subscription_rejects_invalid_country(monkeypatch):
    monkeypatch.setattr(admin_service, "update_subscription", lambda *a, **k: None)
    res = client.patch(
        f"/admin/subscriptions/{_SID}", json={"countries": ["zz"]}, headers=_auth_header()
    )
    assert res.status_code == 422


def test_edit_subscription_requires_token():
    assert (
        client.patch(f"/admin/subscriptions/{_SID}", json={"status": "active"}).status_code == 401
    )


def test_update_subscription_reactivate_stamps_confirmed_at():
    row = _subscription_row(status="paused", confirmed_at=None)
    session = _FakeSession(row)
    out = admin_service.update_subscription(session, _SID, AdminSubscriptionUpdate(status="active"))
    assert out is row
    assert row.status == "active"
    assert row.confirmed_at is not None  # stamped on activation of a never-confirmed row
    assert session.committed


def test_update_subscription_partial_filter_leaves_status(monkeypatch):
    row = _subscription_row(status="active")
    session = _FakeSession(row)
    admin_service.update_subscription(session, _SID, AdminSubscriptionUpdate(countries=["uk"]))
    assert row.countries == ["uk"]
    assert row.status == "active"  # untouched when status omitted


def test_update_subscription_not_found_returns_none():
    assert (
        admin_service.update_subscription(
            _FakeSession(None), _SID, AdminSubscriptionUpdate(status="active")
        )
        is None
    )


# --- message edit ------------------------------------------------------------


def _message_row(**overrides) -> SimpleNamespace:
    base = dict(
        id=1,
        created_at=datetime(2026, 6, 30, tzinfo=UTC),
        message="hello",
        name="Ada",
        email="ada@example.com",
        timezone="UTC",
        locale="en",
        referrer=None,
        user_agent="curl",
        accept_language="en",
        ip_address="1.2.3.4",
        country="US",
        is_bot=False,
        bot_reason=None,
        seen=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_edit_message_returns_updated_row(monkeypatch):
    captured: dict = {}

    def fake(session, message_id, patch):
        captured["id"] = message_id
        captured["patch"] = patch
        return _message_row(seen=True)

    monkeypatch.setattr(admin_service, "update_message", fake)
    res = client.patch("/admin/messages/1", json={"seen": True}, headers=_auth_header())
    assert res.status_code == 200
    assert res.json()["seen"] is True
    assert captured["id"] == 1
    assert captured["patch"].seen is True


def test_edit_message_not_found_is_404(monkeypatch):
    monkeypatch.setattr(admin_service, "update_message", lambda *a, **k: None)
    res = client.patch("/admin/messages/999", json={"seen": True}, headers=_auth_header())
    assert res.status_code == 404


def test_edit_message_requires_seen(monkeypatch):
    monkeypatch.setattr(admin_service, "update_message", lambda *a, **k: None)
    res = client.patch("/admin/messages/1", json={}, headers=_auth_header())
    assert res.status_code == 422


def test_edit_message_requires_token():
    assert client.patch("/admin/messages/1", json={"seen": True}).status_code == 401


def test_update_message_sets_seen_and_commits():
    row = _message_row(seen=False)
    session = _FakeSession(row)
    out = admin_service.update_message(session, 1, AdminMessageUpdate(seen=True))
    assert out is row
    assert row.seen is True
    assert session.committed


def test_update_message_not_found_returns_none():
    assert (
        admin_service.update_message(_FakeSession(None), 1, AdminMessageUpdate(seen=True)) is None
    )


# --- message seen count + filter (DB-backed) ---------------------------------


@pytest.fixture
def db_session():
    # In-memory SQLite with only the messages table — enough to exercise the seen-count and
    # seen-filter SQL without a Postgres dependency (mirrors tests/test_contact_service.py).
    engine = create_engine("sqlite://")
    Message.__table__.create(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as db:
        yield db
    engine.dispose()


def _add_message(session, *, is_bot=False, seen=False):
    session.add(Message(message="hi", is_bot=is_bot, seen=seen))
    session.commit()


def test_message_counts_unseen_excludes_bots_and_seen(db_session):
    _add_message(db_session, seen=False)  # unread real
    _add_message(db_session, seen=True)  # read real
    _add_message(db_session, is_bot=True, seen=False)  # unread bot — must not count
    counts = admin_service._message_counts(db_session)
    assert (counts.total, counts.real, counts.bot, counts.unseen) == (3, 2, 1, 1)


def test_list_messages_seen_filter(db_session):
    _add_message(db_session, seen=False)
    _add_message(db_session, seen=True)
    unread, unread_total = admin_service.list_messages(
        db_session, limit=50, offset=0, include_bots=False, seen=False
    )
    assert unread_total == 1 and all(m.seen is False for m in unread)
    read, read_total = admin_service.list_messages(
        db_session, limit=50, offset=0, include_bots=False, seen=True
    )
    assert read_total == 1 and all(m.seen is True for m in read)
    _, all_total = admin_service.list_messages(
        db_session, limit=50, offset=0, include_bots=False, seen=None
    )
    assert all_total == 2


def test_nullspace_maps_rows_and_total(monkeypatch):
    row = SimpleNamespace(
        id=7,
        created_at=datetime(2026, 6, 30, tzinfo=UTC),
        name="Hacker",
        score=999999,
        kills=1,
        wave=1,
        level=1,
        duration_ms=1000,
        ship_kind="fighter",
        version="1.0.0",
        currency=0,
        space_metal=0,
        upgrades_purchased=0,
        ultimates_owned=0,
        ip_address="9.9.9.9",
        flagged=True,
        flag_reason="score-exceeds-kills",
    )
    monkeypatch.setattr(admin_service, "list_scores", lambda session, **kw: ([row], 1))
    res = client.get("/admin/nullspace", headers=_auth_header())
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["flagged"] is True
    assert body["items"][0]["flagReason"] == "score-exceeds-kills"


def test_nullspace_forwards_flagged_filter(monkeypatch):
    captured: dict = {}

    def fake(session, **kw):
        captured.update(kw)
        return [], 0

    monkeypatch.setattr(admin_service, "list_scores", fake)
    res = client.get("/admin/nullspace?flagged=true&limit=5", headers=_auth_header())
    assert res.status_code == 200
    assert captured == {"limit": 5, "offset": 0, "flagged": True}


def test_nullspace_requires_token():
    assert client.get("/admin/nullspace").status_code == 401


# --- rate limiting -----------------------------------------------------------


def test_login_is_rate_limited():
    limiter.enabled = True
    try:
        for _ in range(5):
            assert client.post("/admin/login", json={"password": PASSWORD}).status_code == 200
        assert client.post("/admin/login", json={"password": PASSWORD}).status_code == 429
    finally:
        limiter.enabled = False
