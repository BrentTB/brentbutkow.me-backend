"""
tests/test_release_suppressed.py
Input parsing for scripts/release_suppressed.py — the operator-run release path.

This script mass-sends, so its two inputs are the only thing standing between "re-send the held
US batch" and "re-send a month of every country". Both are parsed strictly and both are tested.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.release_suppressed import parse_countries, parse_since


def test_parse_since_relative_hours_and_days():
    before = datetime.now(UTC)
    assert timedelta(hours=35, minutes=59) < before - parse_since("36h") <= timedelta(hours=36)
    assert timedelta(days=2, hours=23) < before - parse_since("3d") <= timedelta(days=3)


def test_parse_since_iso_is_always_tz_aware_utc():
    """A naive input must not reach a comparison against tz-aware created_at."""
    assert parse_since("2026-09-08T05:00:00Z") == datetime(2026, 9, 8, 5, 0, tzinfo=UTC)
    assert parse_since("2026-09-08T05:00:00").tzinfo is UTC
    assert parse_since("2026-09-08T07:00:00+02:00") == datetime(2026, 9, 8, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize("value", ["", "soon", "0h", "-3d", "36 hours"])
def test_parse_since_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_since(value)


def test_parse_countries_normalizes_and_dedupes():
    assert parse_countries(" US , us,uk ") == {"us", "uk"}


@pytest.mark.parametrize("value", ["", " , ", "us,xx"])
def test_parse_countries_rejects_empty_or_unknown(value):
    """An unknown code would silently release nothing; an empty list would release everything."""
    with pytest.raises(ValueError):
        parse_countries(value)


def test_release_passes_the_window_and_country_scope_through_to_dispatch(monkeypatch):
    """The bounded window and country set must reach run_dispatch — and the guard must be off."""
    import scripts.release_suppressed as script

    captured: dict = {}

    async def _fake_dispatch(session, **kwargs):
        captured.update(kwargs)
        return {"sent": 0}

    monkeypatch.setattr(script, "SessionLocal", lambda: _NullSession())
    monkeypatch.setattr(script, "run_dispatch", _fake_dispatch)

    since = datetime(2026, 9, 8, 4, 0, tzinfo=UTC)
    until = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
    script.release(since, until, {"us"})

    assert captured == {
        "since": since,
        "until": until,
        "only_countries": {"us"},
        "apply_backfill_guard": False,
    }


class _NullSession:
    def close(self) -> None:
        pass
