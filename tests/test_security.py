import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth import require_bearer
from app.config import settings
from app.modules.recalls import service
from app.rate_limit import client_ip


def _request(headers: dict[str, str], peer: str = "10.0.0.1") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": (peer, 12345)})


def test_require_bearer_accepts_valid_token():
    assert require_bearer(authorization="Bearer test-token") is None


def test_require_bearer_rejects_wrong_token():
    with pytest.raises(HTTPException) as exc:
        require_bearer(authorization="Bearer wrong")
    assert exc.value.status_code == 401


def test_require_bearer_rejects_non_ascii_header():
    # Regression: a non-ASCII header must yield a clean 401, not a 500 — str compare_digest
    # raises TypeError on non-ASCII, so the comparison runs on encoded bytes instead.
    with pytest.raises(HTTPException) as exc:
        require_bearer(authorization="Bearer café")
    assert exc.value.status_code == 401


def test_redact_secrets_strips_api_key(monkeypatch):
    monkeypatch.setattr(settings, "openfda_api_key", "supersecret")
    message = (
        "Client error '429' for url 'https://api.fda.gov/food/enforcement.json?api_key=supersecret'"
    )
    redacted = service._redact_secrets(message)
    assert "supersecret" not in redacted
    assert "***" in redacted


def test_redact_secrets_noop_without_key(monkeypatch):
    monkeypatch.setattr(settings, "openfda_api_key", None)
    assert service._redact_secrets("boom") == "boom"


def test_client_ip_uses_peer_when_no_trusted_proxy(monkeypatch):
    # hops=0: X-Forwarded-For is attacker-controlled and must be ignored.
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    request = _request({"x-forwarded-for": "1.2.3.4"}, peer="10.0.0.1")
    assert client_ip(request) == "10.0.0.1"


def test_client_ip_reads_proxy_controlled_end(monkeypatch):
    # hops=1: a client may forge the left of the chain, but the trusted proxy appends the real
    # peer on the right — taking the rightmost entry resists spoofing.
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    request = _request({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, peer="10.0.0.1")
    assert client_ip(request) == "5.6.7.8"


def test_client_ip_falls_back_when_chain_too_short(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    request = _request({}, peer="10.0.0.1")
    assert client_ip(request) == "10.0.0.1"
