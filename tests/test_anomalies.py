from app.modules.recalls.anomalies import detect_anomalies
from app.modules.recalls.schemas import Anomaly, AnomalyMonth, AnomalyScope
from app.modules.recalls.service import _surface_anomalies


def _months(n: int) -> list[str]:
    out, year, month = [], 2020, 1
    for _ in range(n):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def _series(counts: list[int]) -> list[tuple[str, int]]:
    return list(zip(_months(len(counts)), counts, strict=True))


# A noisy-but-stable baseline, then one obvious spike, then a final (dropped) month.
_BASELINE = [10, 11, 9, 10, 12, 8, 10, 11, 9, 10]


def test_flags_a_clear_spike():
    series = _series(_BASELINE + [100, 10])
    anomalies = detect_anomalies(series)
    assert len(anomalies) == 1
    assert anomalies[0]["observed"] == 100
    assert anomalies[0]["month"] == series[10][0]  # the spike month, not the dropped final one
    assert anomalies[0]["z"] > 0  # positive z = surge


def test_stable_series_has_no_anomalies():
    assert detect_anomalies(_series([10, 11, 9, 10, 12, 8, 10, 11, 9, 10, 11, 10])) == []


def test_final_in_progress_month_is_excluded():
    # The spike lands in the last bucket (a partial current month) — it must NOT be flagged.
    assert detect_anomalies(_series(_BASELINE + [999])) == []


def test_short_history_is_not_scored():
    # Fewer than min_history months behind every point → nothing to baseline against.
    assert detect_anomalies(_series([5, 90, 5, 90, 5])) == []


def test_flat_zero_variance_baseline_is_skipped():
    # No spread in the baseline → no scale to measure against; skip rather than report infinity.
    assert detect_anomalies(_series([10, 10, 10, 10, 10, 10, 10, 50, 10])) == []


def _candidate(label: str, months: list[tuple[str, float]]) -> Anomaly:
    return Anomaly(
        scope=AnomalyScope.entity,
        label=label,
        months=[AnomalyMonth(month=m, observed=1, baseline=1, z=z) for m, z in months],
        series=[],
    )


def test_surface_consolidates_recent_months_and_drops_old():
    recent = {"2025-01", "2025-02", "2025-04"}
    multi = _candidate("Botulinum", [("2024-06", 8.0), ("2025-02", 7.0), ("2025-04", 12.0)])
    other = _candidate("Listeria", [("2025-01", 9.0)])
    old = _candidate("AncientThing", [("2019-03", 50.0)])

    surfaced = _surface_anomalies([multi, other, old], recent, limit=8)

    # AncientThing has no recent month → dropped; the rest are ordered newest-first by latest flag.
    assert [a.label for a in surfaced] == ["Botulinum", "Listeria"]
    # One consolidated card carries all its recent months; the out-of-window 2024-06 is trimmed.
    assert [m.month for m in surfaced[0].months] == ["2025-02", "2025-04"]


def test_surface_caps_by_peak_severity():
    recent = {"2025-01", "2025-02", "2025-03"}
    candidates = [
        _candidate("Strong", [("2025-02", 9.9)]),
        _candidate("Mid", [("2025-03", 5.0)]),
        _candidate("Weak", [("2025-01", 3.1)]),
    ]
    # limit 2 keeps the two strongest things (Strong, Mid), then orders them newest-first.
    surfaced = _surface_anomalies(candidates, recent, limit=2)
    assert [a.label for a in surfaced] == ["Mid", "Strong"]
