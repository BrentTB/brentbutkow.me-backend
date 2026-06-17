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


def test_flat_baseline_still_flags_a_real_spike():
    # A perfectly flat baseline has zero spread, but the scale floor gives it a sane denominator,
    # so a genuine spike (10 → 50) is still caught instead of being silently skipped.
    anomalies = detect_anomalies(_series([10, 10, 10, 10, 10, 10, 10, 50, 10]))
    assert len(anomalies) == 1
    assert anomalies[0]["observed"] == 50


def test_dips_are_not_flagged():
    # One-sided: a sharp drop (10 → 0) is not an anomaly, even though |z| is huge. Otherwise it
    # renders as a near-zero "highlighted" bar that no reader recognises as flagged.
    assert detect_anomalies(_series(_BASELINE + [0, 10])) == []


def test_tiny_absolute_move_on_a_quiet_series_is_not_flagged():
    # Sparse near-zero category: a 0 → 2 bump is statistically large but absolutely trivial, so the
    # absolute-rise floor suppresses it — while a real 0 → 3 spike still flags.
    assert detect_anomalies(_series([0] * 12 + [2, 0])) == []
    flagged = detect_anomalies(_series([0] * 12 + [3, 0]))
    assert [a["observed"] for a in flagged] == [3]


def test_equal_heights_after_a_spike_are_treated_consistently():
    # Regression: a quiet series spikes 2 → 3 → 2. The two 2s are identical, so neither should
    # flag; only the 3 clears the floors. (Previously the first 2 flagged and the second didn't,
    # because the spike inflated the trailing stddev — the bug this detector now guards against.)
    flagged = detect_anomalies(_series([0] * 12 + [2, 3, 2, 1, 0]))
    assert [a["observed"] for a in flagged] == [3]


def test_near_record_high_flags_even_when_local_baseline_is_too_noisy_for_sigma():
    # A choppy 5/15 baseline (spread ≈ 7) means a record-high 22 is only ~1.6σ — the relative test
    # alone would miss it. The near-record rule catches it anyway: it's well above the series' 90th
    # percentile and clears the absolute-rise floor.
    flagged = detect_anomalies(_series([5, 15] * 6 + [22, 10]))
    assert [a["observed"] for a in flagged] == [22]
    assert flagged[0]["z"] < 3.0  # flagged on magnitude, not on a 3σ spike


def test_high_but_flat_plateau_never_flags():
    # Every month is the all-time high, so the near-record percentile is always cleared — but none
    # rises above the (equally high) baseline, so the absolute-rise floor keeps the plateau quiet.
    assert detect_anomalies(_series([20] * 20)) == []


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
