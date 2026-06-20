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


def test_list_recalls_forwards_entity(monkeypatch):
    captured: dict = {}

    def fake_list(*a, **k):
        captured.update(k)
        return {"items": [], "total": 0}

    monkeypatch.setattr(router_module, "list_recalls", fake_list)
    assert client.get("/recalls?entity=peanuts").status_code == 200
    assert captured["entity"] == "peanuts"
    # over-length entity is rejected by the 100-char bound
    assert client.get("/recalls?entity=" + "x" * 101).status_code == 422


def test_list_recalls_forwards_sort_and_min_severity(monkeypatch):
    captured: dict = {}

    def fake_list(*a, **k):
        captured.clear()
        captured.update(k)
        return {"items": [], "total": 0}

    monkeypatch.setattr(router_module, "list_recalls", fake_list)
    assert client.get("/recalls?sort=severity&minSeverity=70").status_code == 200
    assert captured["sort"] == "severity"
    assert captured["min_severity"] == 70
    # sort defaults to recency when omitted
    assert client.get("/recalls").status_code == 200
    assert captured["sort"] == "recency"
    # an unknown sort is rejected by the enum; out-of-range severity by the 0–100 bound
    assert client.get("/recalls?sort=oldest").status_code == 422
    assert client.get("/recalls?minSeverity=150").status_code == 422


def test_list_recalls_forwards_severity(monkeypatch):
    captured: dict = {}

    def fake_list(*a, **k):
        captured.clear()
        captured.update(k)
        return {"items": [], "total": 0}

    monkeypatch.setattr(router_module, "list_recalls", fake_list)
    assert client.get("/recalls?severity=severe").status_code == 200
    assert captured["severity"] == "severe"
    # an unknown band is rejected by the enum, not silently forwarded
    assert client.get("/recalls?severity=critical").status_code == 422


def test_list_recalls_forwards_topic(monkeypatch):
    captured: dict = {}

    def fake_list(*a, **k):
        captured.clear()
        captured.update(k)
        return {"items": [], "total": 0}

    monkeypatch.setattr(router_module, "list_recalls", fake_list)
    # topic + event are stable slugs (strings), not volatile numeric ids.
    assert client.get("/recalls?topic=listeria-deli-meat").status_code == 200
    assert captured["topic"] == "listeria-deli-meat"
    assert client.get("/recalls?event=listeria-2026-03").status_code == 200
    assert captured["event"] == "listeria-2026-03"


def test_topics(monkeypatch):
    monkeypatch.setattr(router_module, "get_topics", lambda *a, **k: [])
    res = client.get("/recalls/topics")
    assert res.status_code == 200
    assert res.json() == []
    assert res.headers["cache-control"] == "public, max-age=300"
    # themes can be scoped to a country; an unknown one is rejected by the enum
    assert client.get("/recalls/topics?country=uk").status_code == 200


def test_events(monkeypatch):
    from app.modules.recalls.schemas import EventOut

    event = EventOut(
        id=1,
        slug="listeria-2026-03",
        label="Listeria · 7 recalls",
        is_outbreak=True,
        dominant_entity="Listeria",
        recall_count=7,
        company_count=3,
        state_count=4,
        severity_max=92.0,
    )
    monkeypatch.setattr(router_module, "get_events", lambda *a, **k: [event])
    res = client.get("/recalls/events")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "public, max-age=300"
    body = res.json()
    # EventOut serialises camelCase, like every other DTO.
    assert body[0]["isOutbreak"] is True
    assert body[0]["dominantEntity"] == "Listeria"
    assert body[0]["recallCount"] == 7

    # The camelCase `outbreaksOnly` query param maps onto the service's outbreaks_only kwarg.
    captured: dict = {}
    monkeypatch.setattr(router_module, "get_events", lambda *a, **k: captured.update(k) or [])
    assert client.get("/recalls/events?outbreaksOnly=true").status_code == 200
    assert captured["outbreaks_only"] is True
    assert client.get("/recalls/topics?country=zz").status_code == 422


def test_similar(monkeypatch):
    monkeypatch.setattr(router_module, "get_similar", lambda *a, **k: [])
    res = client.get("/recalls/fda/F-1/similar")
    assert res.status_code == 200
    assert res.json() == []
    # an unknown source is rejected by the enum
    assert client.get("/recalls/epa/F-1/similar").status_code == 422
    # the limit is bounded (1–20)
    assert client.get("/recalls/fda/F-1/similar?limit=50").status_code == 422


def test_stats(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "get_stats",
        lambda *a, **k: {
            "total": 0,
            "by_category": [],
            "by_month": [],
            "by_classification": [],
            "by_severity": [],
            "by_state": [],
            "by_company": [],
            "by_source": [],
            "by_entity": [],
            "anomalies": [],
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


def test_trend(monkeypatch):
    monkeypatch.setattr(
        router_module, "get_trend", lambda *a, **k: {"group": "category", "buckets": []}
    )
    res = client.get("/recalls/trend?group=category")
    assert res.status_code == 200
    assert res.json() == {"group": "category", "buckets": []}
    assert res.headers["cache-control"] == "public, max-age=300"
    # group defaults to total; an unknown group is rejected by the enum
    assert client.get("/recalls/trend").status_code == 200
    assert client.get("/recalls/trend?group=state").status_code == 422


def test_ingest_requires_bearer():
    assert client.post("/recalls/ingest/fda").status_code == 401


def test_ingest_with_bearer(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "run_fda_ingest",
        lambda *a, **k: {"status": "ok", "fetched": 3, "new": 1, "upserted": 3},
    )
    res = client.post("/recalls/ingest/fda", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "fetched": 3, "new": 1, "upserted": 3}


def test_ingest_fsis_requires_bearer():
    assert client.post("/recalls/ingest/fsis").status_code == 401


def test_ingest_fsis_with_bearer(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "run_fsis_ingest",
        lambda *a, **k: {"status": "ok", "fetched": 2, "new": 2, "upserted": 2},
    )
    res = client.post("/recalls/ingest/fsis", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "fetched": 2, "new": 2, "upserted": 2}


def test_ingest_uk_requires_bearer():
    assert client.post("/recalls/ingest/uk").status_code == 401


def test_ingest_uk_with_bearer(monkeypatch):
    monkeypatch.setattr(
        router_module,
        "run_uk_ingest",
        lambda *a, **k: {"status": "ok", "fetched": 1, "new": 1, "upserted": 1},
    )
    res = client.post("/recalls/ingest/uk", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "fetched": 1, "new": 1, "upserted": 1}
