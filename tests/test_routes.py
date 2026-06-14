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
    assert res.headers["cache-control"] == "public, max-age=120"
    # the filter params are accepted (FastAPI-validated) and don't error
    assert client.get("/recalls?state=CA&company=Acme&category=allergen").status_code == 200


def test_list_recalls_forwards_search(monkeypatch):
    captured: dict = {}

    def fake_list(*a, **k):
        captured.update(k)
        return {"items": [], "total": 0}

    monkeypatch.setattr(router_module, "list_recalls", fake_list)
    assert client.get("/recalls?search=listeria").status_code == 200
    assert captured["search"] == "listeria"


def test_search_edge_cases(monkeypatch):
    captured: dict = {}

    def fake_list(*a, **k):
        captured.clear()
        captured.update(k)
        return {"items": [], "total": 0}

    monkeypatch.setattr(router_module, "list_recalls", fake_list)
    # special tsquery characters are accepted and forwarded verbatim (the service binds them safely)
    assert client.get("/recalls?search=%26%7C%21listeria").status_code == 200
    assert captured["search"] == "&|!listeria"
    # whitespace-only is accepted (the service normalizes it to "no search")
    assert client.get("/recalls?search=%20%20").status_code == 200
    # over-length terms are rejected by the 200-char bound, not pushed into the query
    assert client.get("/recalls?search=" + "x" * 201).status_code == 422


def test_list_recalls_forwards_source(monkeypatch):
    captured: dict = {}

    def fake_list(*a, **k):
        captured.update(k)
        return {"items": [], "total": 0}

    monkeypatch.setattr(router_module, "list_recalls", fake_list)
    assert client.get("/recalls?source=usda").status_code == 200
    assert captured["source"] == "usda"
    # an unknown source is rejected by the enum, not silently forwarded
    assert client.get("/recalls?source=epa").status_code == 422


def test_list_recalls_forwards_country(monkeypatch):
    captured: dict = {}

    def fake_list(*a, **k):
        captured.update(k)
        return {"items": [], "total": 0}

    monkeypatch.setattr(router_module, "list_recalls", fake_list)
    assert client.get("/recalls?country=uk").status_code == 200
    assert captured["country"] == "uk"
    # an unknown country is rejected by the enum, not silently forwarded
    assert client.get("/recalls?country=narnia").status_code == 422


def test_stats(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "get_stats",
        lambda *a, **k: {
            "total": 0,
            "by_category": [],
            "by_month": [],
            "by_classification": [],
            "by_state": [],
            "by_company": [],
            "by_source": [],
            "last_ingest_at": None,
        },
    )
    res = client.get("/recalls/stats")
    assert res.status_code == 200
    assert res.json()["total"] == 0
    assert res.headers["cache-control"] == "public, max-age=300"
    # stats can be scoped to a country; an unknown one is rejected
    assert client.get("/recalls/stats?country=uk").status_code == 200
    assert client.get("/recalls/stats?country=zz").status_code == 422


def test_ingest_requires_bearer():
    assert client.post("/recalls/ingest/fda").status_code == 401


def test_ingest_with_bearer(monkeypatch):
    monkeypatch.setattr(
        router_module, "run_ingest", lambda *a, **k: {"status": "ok", "fetched": 3, "upserted": 3}
    )
    res = client.post("/recalls/ingest/fda", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "fetched": 3, "upserted": 3}
