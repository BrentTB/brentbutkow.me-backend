from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.db import get_session
from app.internal import router as internal_router_module
from app.main import app
from app.subscriptions import service

# Routes are exercised without a database: the session dependency is stubbed and the service layer
# (tested separately) is monkeypatched, so these tests only assert request → status-code wiring.
app.dependency_overrides[get_session] = lambda: None
client = TestClient(app)

_VALID_BODY = {"email": "a@b.com", "countries": ["us"], "entities": ["peanut"]}


def test_create_forwards_service_status(monkeypatch):
    monkeypatch.setattr(service, "create", lambda payload, db: (201, {"message": "created"}))
    res = client.post("/subscriptions", json=_VALID_BODY)
    assert res.status_code == 201
    assert res.json() == {"message": "created"}


def test_create_duplicate_returns_409(monkeypatch):
    monkeypatch.setattr(service, "create", lambda payload, db: (409, {"detail": "duplicate"}))
    assert client.post("/subscriptions", json=_VALID_BODY).status_code == 409


def test_create_rejects_invalid_email():
    res = client.post("/subscriptions", json={**_VALID_BODY, "email": "not-an-email"})
    assert res.status_code == 422


def test_create_rejects_empty_countries():
    res = client.post(
        "/subscriptions", json={"email": "a@b.com", "countries": [], "entities": ["x"]}
    )
    assert res.status_code == 422


def test_create_accepts_countries_only(monkeypatch):
    # No other filter is required — a countries-only body passes validation and reaches the service.
    monkeypatch.setattr(service, "create", lambda payload, db: (200, {"message": "ok"}))
    res = client.post("/subscriptions", json={"email": "a@b.com", "countries": ["us"]})
    assert res.status_code == 200


def test_confirm_requires_token():
    assert client.get("/subscriptions/confirm").status_code == 422


def test_confirm_forwards_service_status(monkeypatch):
    monkeypatch.setattr(service, "confirm", lambda token, db: (200, {"message": "confirmed"}))
    res = client.get("/subscriptions/confirm", params={"token": "raw"})
    assert res.status_code == 200
    assert res.json() == {"message": "confirmed"}


def test_manage_requires_token():
    assert client.get("/subscriptions/manage").status_code == 422


def test_manage_forwards_service_status(monkeypatch):
    monkeypatch.setattr(service, "get_manage", lambda token, db: (410, {"detail": "gone"}))
    assert client.get("/subscriptions/manage", params={"token": "t"}).status_code == 410


def test_patch_manage_forwards_service_status(monkeypatch):
    monkeypatch.setattr(
        service, "patch_manage", lambda token, patch, db: (200, {"status": "active"})
    )
    res = client.patch("/subscriptions/manage", params={"token": "t"}, json={"companies": ["Acme"]})
    assert res.status_code == 200
    assert res.json() == {"status": "active"}


def test_unsubscribe_requires_token():
    assert client.post("/subscriptions/unsubscribe").status_code == 422


def test_unsubscribe_forwards_service_status(monkeypatch):
    monkeypatch.setattr(service, "unsubscribe", lambda token, db: (200, {"message": "bye"}))
    assert client.post("/subscriptions/unsubscribe", params={"token": "t"}).status_code == 200


# ---------------------------------------------------------------------------
# Internal dispatch trigger
# ---------------------------------------------------------------------------


async def _fake_dispatch(session):
    return {"newRecalls": 0, "activeSubs": 0, "sent": 0, "skippedCap": 0, "errors": 0}


def _reset_lock():
    internal_router_module._lock_started_at = None


def test_dispatch_rejects_missing_token():
    assert client.post("/internal/dispatch-alerts").status_code == 403


def test_dispatch_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_dispatch_token", "secret")
    res = client.post("/internal/dispatch-alerts", headers={"X-Internal-Token": "nope"})
    assert res.status_code == 403


def test_dispatch_rejects_when_secret_unset(monkeypatch):
    # Fail closed: with no configured secret, even a matching-looking token is refused.
    monkeypatch.setattr(settings, "internal_dispatch_token", None)
    res = client.post("/internal/dispatch-alerts", headers={"X-Internal-Token": ""})
    assert res.status_code == 403


def test_dispatch_runs_with_valid_token(monkeypatch):
    monkeypatch.setattr(settings, "internal_dispatch_token", "secret")
    monkeypatch.setattr(internal_router_module, "run_dispatch", _fake_dispatch)
    _reset_lock()
    res = client.post("/internal/dispatch-alerts", headers={"X-Internal-Token": "secret"})
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    # Lock is released after the run so the next trigger isn't blocked.
    assert internal_router_module._lock_started_at is None


def test_dispatch_returns_409_while_in_progress(monkeypatch):
    monkeypatch.setattr(settings, "internal_dispatch_token", "secret")
    internal_router_module._lock_started_at = datetime.now(UTC)
    try:
        res = client.post("/internal/dispatch-alerts", headers={"X-Internal-Token": "secret"})
        assert res.status_code == 409
    finally:
        _reset_lock()


def test_dispatch_reclaims_stale_lock(monkeypatch):
    monkeypatch.setattr(settings, "internal_dispatch_token", "secret")
    monkeypatch.setattr(internal_router_module, "run_dispatch", _fake_dispatch)
    # A lock older than the 10-minute TTL is stale (crashed run) — the next trigger reclaims it.
    internal_router_module._lock_started_at = datetime.now(UTC) - timedelta(minutes=11)
    res = client.post("/internal/dispatch-alerts", headers={"X-Internal-Token": "secret"})
    assert res.status_code == 200
    _reset_lock()
