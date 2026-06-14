from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.main import create_app

PREVIEW_REGEX = r"https://[a-z0-9-]+\.preview\.example\.com"


def _client(monkeypatch, regex: str) -> TestClient:
    # CORSMiddleware bakes the origin/regex in at construction, so build a fresh app after setting
    # the value rather than reusing the module-level singleton.
    monkeypatch.setattr(settings, "allowed_origin_regex", regex)
    return TestClient(create_app())


def test_origin_matching_regex_is_allowed(monkeypatch):
    client = _client(monkeypatch, PREVIEW_REGEX)
    origin = "https://feature-x.preview.example.com"
    res = client.get("/health", headers={"Origin": origin})
    assert res.headers["access-control-allow-origin"] == origin


def test_origin_not_matching_regex_is_denied(monkeypatch):
    # A blanket-looking site outside the anchored scope must not be reflected back.
    client = _client(monkeypatch, PREVIEW_REGEX)
    res = client.get("/health", headers={"Origin": "https://evil.com"})
    assert "access-control-allow-origin" not in res.headers


def test_configured_exact_origin_still_allowed_alongside_regex(monkeypatch):
    # The regex is additive: the explicitly-listed ALLOWED_ORIGIN must still pass.
    client = _client(monkeypatch, PREVIEW_REGEX)
    origin = settings.origins[0]
    res = client.get("/health", headers={"Origin": origin})
    assert res.headers["access-control-allow-origin"] == origin


def test_blank_regex_normalizes_to_none():
    # `ALLOWED_ORIGIN_REGEX=` (blank) must collapse to None, not an empty compiled regex that
    # would run on every request and match nothing.
    assert Settings(allowed_origin_regex="").allowed_origin_regex is None
    assert Settings(allowed_origin_regex="   ").allowed_origin_regex is None
    assert Settings(allowed_origin_regex=PREVIEW_REGEX).allowed_origin_regex == PREVIEW_REGEX
