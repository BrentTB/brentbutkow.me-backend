"""
Settings loading, and the one rule that is easy to get wrong: a blank value means "use the default".

`op inject` renders a 1Password field holding nothing as an empty string, so a template line left
blank on purpose arrives as `SETTING=`. Without the rule, an int setting fails to parse and the app
does not start at all — `settings = Settings()` runs at import.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED = {"DATABASE_URL": "postgresql://user@host/db", "INGEST_BEARER_TOKEN": "token"}


def _load(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name, value in {**REQUIRED, **env}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _default(field: str) -> object:
    return Settings.model_fields[field].default


@pytest.mark.parametrize(
    "field",
    ["room_presence_timeout_seconds", "room_forfeit_timeout_seconds", "room_ttl_seconds"],
)
def test_a_blank_numeric_setting_falls_back_to_its_default(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    settings = _load(monkeypatch, **{field.upper(): ""})

    assert getattr(settings, field) == _default(field)


def test_a_whitespace_only_setting_counts_as_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load(monkeypatch, ROOM_FORFEIT_TIMEOUT_SECONDS="   ")

    assert settings.room_forfeit_timeout_seconds == _default("room_forfeit_timeout_seconds")


def test_a_real_value_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load(monkeypatch, ROOM_FORFEIT_TIMEOUT_SECONDS="45")

    assert settings.room_forfeit_timeout_seconds == 45


def test_a_blank_optional_string_lands_on_none_not_an_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty ALLOWED_ORIGIN_REGEX would otherwise compile into a regex run on every request.
    settings = _load(monkeypatch, ALLOWED_ORIGIN_REGEX="", RESEND_API_KEY="")

    assert settings.allowed_origin_regex is None
    assert settings.resend_api_key is None


def test_a_blank_required_setting_reads_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_BEARER_TOKEN", "token")
    monkeypatch.setenv("DATABASE_URL", "")

    with pytest.raises(ValidationError) as raised:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert "database_url" in str(raised.value)
    assert "Field required" in str(raised.value)
