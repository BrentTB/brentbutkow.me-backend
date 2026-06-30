from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.auth import issue_admin_token, verify_admin_token
from app.config import settings
from app.db import get_session
from app.main import app
from app.modules.admin import service as admin_service
from app.modules.admin.schemas import (
    AdminOverview,
    IngestSummary,
    MessageCounts,
    NullspaceCounts,
    RecallCounts,
    SubscriptionCounts,
)
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
        messages=MessageCounts(total=3, real=2, bot=1),
        subscriptions=SubscriptionCounts(
            total=5, active=4, pending_confirmation=1, paused=0, unsubscribed=0
        ),
        ingest=IngestSummary(
            last_run_at=datetime(2026, 6, 30, tzinfo=UTC),
            status="success",
            fetched_count=10,
            upserted_count=7,
        ),
        recalls=RecallCounts(total=100, us=80, uk=15, za=5),
        nullspace=NullspaceCounts(score_count=42),
    )


def test_overview_returns_summary(monkeypatch):
    monkeypatch.setattr(admin_service, "build_overview", lambda session: _fake_overview())
    res = client.get("/admin/overview", headers=_auth_header())
    assert res.status_code == 200
    body = res.json()
    assert body["messages"] == {"total": 3, "real": 2, "bot": 1}
    assert body["subscriptions"]["active"] == 4
    assert body["recalls"]["us"] == 80
    assert body["nullspace"]["scoreCount"] == 42


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
    )
    monkeypatch.setattr(admin_service, "list_messages", lambda session, **kw: ([row], 1))
    res = client.get("/admin/messages", headers=_auth_header())
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["message"] == "hello"
    assert body["items"][0]["isBot"] is False


def test_messages_forwards_pagination_and_bot_flag(monkeypatch):
    captured: dict = {}

    def fake(session, **kw):
        captured.update(kw)
        return [], 0

    monkeypatch.setattr(admin_service, "list_messages", fake)
    res = client.get("/admin/messages?limit=10&offset=20&includeBots=true", headers=_auth_header())
    assert res.status_code == 200
    assert captured == {"limit": 10, "offset": 20, "include_bots": True}


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


# --- rate limiting -----------------------------------------------------------


def test_login_is_rate_limited():
    limiter.enabled = True
    try:
        for _ in range(5):
            assert client.post("/admin/login", json={"password": PASSWORD}).status_code == 200
        assert client.post("/admin/login", json={"password": PASSWORD}).status_code == 429
    finally:
        limiter.enabled = False
