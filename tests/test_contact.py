from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.modules.contact import router as contact_router_module
from app.rate_limit import limiter

app.dependency_overrides[get_session] = lambda: None
client = TestClient(app)


def test_submit_stores_message_with_server_metadata(monkeypatch):
    captured: dict = {}

    def fake_create(session, submission, **meta):
        captured["submission"] = submission
        captured["meta"] = meta

    monkeypatch.setattr(contact_router_module, "create_message", fake_create)
    res = client.post(
        "/contact",
        json={"message": "Hello there", "email": "a@b.com", "timezone": "Africa/Johannesburg"},
    )
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    assert captured["submission"].message == "Hello there"
    assert captured["submission"].timezone == "Africa/Johannesburg"
    # server fills the request-derived context the client can't be trusted to send
    assert captured["meta"]["ip_address"]
    assert "userAgent" not in captured["meta"]  # stored under snake_case kwarg
    assert "user_agent" in captured["meta"]


def test_honeypot_is_stored_flagged_as_bot(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        contact_router_module,
        "create_message",
        lambda session, submission, **meta: captured.update(meta),
    )
    res = client.post("/contact", json={"message": "spam", "website": "http://spam.example"})
    assert res.status_code == 200
    assert captured["is_bot"] is True
    assert captured["bot_reason"] == "honeypot"


def test_time_trap_flags_instant_submissions_as_bot(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        contact_router_module,
        "create_message",
        lambda session, submission, **meta: captured.update(meta),
    )
    res = client.post("/contact", json={"message": "hi", "elapsedMs": 50})
    assert res.status_code == 200
    assert captured["is_bot"] is True
    assert captured["bot_reason"] == "timetrap"


def test_blank_message_is_rejected():
    assert client.post("/contact", json={"message": "   "}).status_code == 422


def test_invalid_email_is_rejected():
    assert client.post("/contact", json={"message": "hi", "email": "notanemail"}).status_code == 422


def test_post_contact_is_rate_limited(monkeypatch):
    # The limiter is disabled suite-wide (conftest) to keep tests order-independent; re-enable it
    # here since it's the primary anti-abuse control on the public POST. store() is stubbed so the
    # check is exercised without a DB.
    monkeypatch.setattr(contact_router_module, "create_message", lambda *a, **k: None)
    limiter.enabled = True
    try:
        body = {"message": "Hello there", "elapsedMs": 5000}
        for _ in range(5):
            assert client.post("/contact", json=body).status_code == 200
        # 6th request in the window trips the 5/min per-IP limit.
        assert client.post("/contact", json=body).status_code == 429
    finally:
        limiter.enabled = False
