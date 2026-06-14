from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.modules.recalls import router as router_module

# Override the DB dependency and patch the service so routes are tested without a database.
app.dependency_overrides[get_session] = lambda: None
client = TestClient(app)


def test_health_ok_and_not_rate_limited():
    # /health is exempt from the global 60/min limit, so liveness probes never get a 429.
    for _ in range(70):
        res = client.get("/health")
        assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_list_recalls(monkeypatch):
    monkeypatch.setattr(router_module, "list_recalls", lambda *a, **k: {"items": [], "total": 0})
    res = client.get("/recalls")
    assert res.status_code == 200
    assert res.json() == {"items": [], "total": 0}


def test_stats(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "get_stats",
        lambda *a, **k: {"total": 0, "by_category": [], "by_month": [], "last_ingest_at": None},
    )
    res = client.get("/recalls/stats")
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_ingest_requires_bearer():
    assert client.post("/recalls/ingest").status_code == 401


def test_ingest_with_bearer(monkeypatch):
    monkeypatch.setattr(
        router_module, "run_ingest", lambda *a, **k: {"status": "ok", "fetched": 3, "upserted": 3}
    )
    res = client.post("/recalls/ingest", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "fetched": 3, "upserted": 3}
