from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.modules.nullspace import router as nullspace_router_module
from app.modules.nullspace.models import Score
from app.modules.nullspace.schemas import ScoreSubmission
from app.modules.nullspace.service import evaluate_submission
from app.rate_limit import limiter

app.dependency_overrides[get_session] = lambda: None
client = TestClient(app)

# A believable finished run, camelCase on the wire exactly as the game posts it.
VALID = {
    "name": "ACE",
    "score": 1000,
    "kills": 10,
    "wave": 5,
    "level": 2,
    "durationMs": 60_000,
    "shipKind": "fighter",
    "version": "1.2.0",
    "currency": 50,
    "spaceMetal": 2,
    "upgradesPurchased": 3,
    "ultimatesOwned": 0,
}


def _body(**over):
    return {**VALID, **over}


def test_valid_score_is_stored_unflagged(monkeypatch):
    captured: dict = {}

    def fake_create(session, submission, **meta):
        captured["submission"] = submission
        captured.update(meta)

    monkeypatch.setattr(nullspace_router_module, "create_score", fake_create)
    res = client.post("/nullspace/score", json=_body())
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    assert captured["flagged"] is False
    assert captured["flag_reason"] is None
    assert captured["submission"].ship_kind == "fighter"
    # server fills the IP the client can't be trusted to send
    assert captured["ip_address"]


def test_blatant_score_is_accepted_but_flagged(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        nullspace_router_module,
        "create_score",
        lambda session, submission, **meta: captured.update(meta),
    )
    # A console-posted 9,999,999 with no kills: accepted (no signal) but flagged.
    res = client.post("/nullspace/score", json=_body(score=9_999_999, kills=0))
    assert res.status_code == 200
    assert captured["flagged"] is True
    assert captured["flag_reason"] == "score-exceeds-kills"


def test_negative_score_is_rejected():
    assert client.post("/nullspace/score", json=_body(score=-1)).status_code == 422


def test_unknown_ship_kind_is_rejected():
    assert client.post("/nullspace/score", json=_body(shipKind="x-wing")).status_code == 422


def test_blank_version_is_rejected():
    assert client.post("/nullspace/score", json=_body(version="   ")).status_code == 422


def test_leaderboard_serializes_camelcase_and_hides_internal_fields(monkeypatch):
    row = Score(
        id=1,
        created_at=datetime(2026, 6, 19, tzinfo=UTC),
        name="ACE",
        score=1000,
        kills=10,
        wave=5,
        level=2,
        ship_kind="fighter",
        version="1.2.0",
        currency=50,
        space_metal=2,
        upgrades_purchased=3,
        ultimates_owned=0,
        ip_address="1.2.3.4",
        flagged=False,
        flag_reason=None,
    )
    monkeypatch.setattr(nullspace_router_module, "list_scores", lambda session, **kw: [row])
    res = client.get("/nullspace/leaderboard")
    assert res.status_code == 200
    item = res.json()[0]
    assert item["shipKind"] == "fighter"  # camelCase alias on the wire
    assert item["score"] == 1000
    # IP and the flag internals are never served
    assert "ipAddress" not in item
    assert "flagged" not in item


def test_leaderboard_limit_is_capped(monkeypatch):
    # `limit` is bounded by the route (Query le=200), so an over-cap value is a 422 — never an
    # unbounded scan reachable on the public endpoint.
    monkeypatch.setattr(nullspace_router_module, "list_scores", lambda session, **kw: [])
    assert client.get("/nullspace/leaderboard?limit=201").status_code == 422
    assert client.get("/nullspace/leaderboard?limit=0").status_code == 422


def test_leaderboard_forwards_version_and_limit(monkeypatch):
    captured: dict = {}

    def fake_list(session, **kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(nullspace_router_module, "list_scores", fake_list)
    res = client.get("/nullspace/leaderboard?version=1.2.0&limit=10")
    assert res.status_code == 200
    # version + limit reach the service unchanged (the scoping the route promises).
    assert captured["version"] == "1.2.0"
    assert captured["limit"] == 10


def test_post_is_rate_limited(monkeypatch):
    # The limiter is disabled suite-wide (conftest); re-enable here since it's the
    # primary anti-abuse control on the public POST. create_score is stubbed so the
    # check is exercised without a DB.
    monkeypatch.setattr(nullspace_router_module, "create_score", lambda *a, **k: None)
    limiter.enabled = True
    try:
        for _ in range(10):
            assert client.post("/nullspace/score", json=_body()).status_code == 200
        # 11th request in the window trips the 10/min per-IP limit.
        assert client.post("/nullspace/score", json=_body()).status_code == 429
    finally:
        limiter.enabled = False


# --- evaluate_submission: pure plausibility, no DB ---


def _sub(**over) -> ScoreSubmission:
    base = dict(
        name="ACE",
        score=1000,
        kills=10,
        wave=5,
        level=2,
        duration_ms=60_000,
        ship_kind="fighter",
        version="1.2.0",
        currency=50,
        space_metal=2,
        upgrades_purchased=3,
        ultimates_owned=0,
    )
    base.update(over)
    return ScoreSubmission(**base)


def test_plausible_run_passes():
    assert evaluate_submission(_sub()) == (False, None)


def test_score_far_above_kills_is_flagged():
    assert evaluate_submission(_sub(score=9_999_999, kills=0)) == (True, "score-exceeds-kills")


def test_kills_far_above_wave_is_flagged():
    assert evaluate_submission(_sub(score=0, kills=10_000, wave=1)) == (True, "kills-exceed-wave")


def test_run_too_fast_for_wave_is_flagged():
    result = evaluate_submission(_sub(score=0, kills=0, wave=60, duration_ms=1_000))
    assert result == (True, "too-fast-for-wave")
