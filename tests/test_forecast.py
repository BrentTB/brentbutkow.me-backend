from app.modules.recalls.forecast import _add_months, _month_number, forecast_series


def _months(start: str, counts: list[int]) -> list[tuple[str, int]]:
    # Consecutive (YYYY-MM, count) from `start`, one per entry in `counts`.
    year, month = int(start[:4]), int(start[5:7])
    out: list[tuple[str, int]] = []
    for count in counts:
        out.append((f"{year:04d}-{month:02d}", count))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def _seasonal(n: int, base: int, spike_month: int, spike: int, start_month: int = 1) -> list[int]:
    # Flat baseline with a fixed lift on one calendar month — a clean annual season to recover.
    counts = []
    for i in range(n):
        cal = (start_month - 1 + i) % 12 + 1
        counts.append(base + (spike if cal == spike_month else 0))
    return counts


def test_short_series_returns_no_forecast():
    # Fewer than 24 months of history (after dropping the in-progress month) → no projection.
    assert forecast_series(_months("2020-01", [5] * 20)) == []


def test_min_history_boundary():
    # 24 months of points → 23 after the drop, still short. 25 → 24, exactly enough.
    assert forecast_series(_months("2020-01", [5] * 24)) == []
    assert forecast_series(_months("2020-01", [5] * 25)) != []


def test_sparse_series_returns_no_forecast():
    # A low-volume country (South Africa: a handful of recalls over years) has enough *length* but
    # near-zero monthly volume — a seasonal fit there is noise, so no projection is returned.
    sparse = [1 if i % 5 == 0 else 0 for i in range(40)]  # ~0.2/month
    assert sum(sparse) / len(sparse) < 1
    assert forecast_series(_months("2021-01", sparse)) == []
    # Same length, real volume → a forecast is produced. Only density gates it.
    assert forecast_series(_months("2021-01", [12] * 40)) != []


def test_horizon_length():
    series = _months("2022-01", _seasonal(40, 10, 6, 8))
    assert len(forecast_series(series, horizon=3)) == 3
    assert len(forecast_series(series, horizon=6)) == 6


def test_forecast_covers_the_current_month_forward():
    # Fitting drops the in-progress final month, but the horizon's first point re-projects it.
    series = _months("2022-01", _seasonal(31, 10, 6, 8))  # ends 2024-07 (in progress)
    assert forecast_series(series)[0]["month"] == "2024-07"


def test_in_progress_month_does_not_bias_the_fit():
    # The final month is dropped before fitting, so a wild partial count can't move the forecast.
    base = _months("2022-01", [10] * 30)
    calm = base + [("2024-07", 10)]
    spike = base + [("2024-07", 9999)]
    assert forecast_series(calm) == forecast_series(spike)


def test_recovers_seasonality():
    # A +30 December lift should make projected Decembers clearly outrank a trough month.
    series = _months("2022-01", _seasonal(49, 10, 12, 30))
    forecast = forecast_series(series, horizon=12)
    december = next(p for p in forecast if p["month"].endswith("-12"))
    june = next(p for p in forecast if p["month"].endswith("-06"))
    assert december["predicted"] > june["predicted"] + 15


def test_extends_an_upward_trend():
    # A steady +2/month climb should keep climbing across the horizon.
    series = _months("2022-01", [5 + 2 * i for i in range(40)])
    forecast = forecast_series(series, horizon=3)
    predictions = [p["predicted"] for p in forecast]
    assert predictions[0] < predictions[1] < predictions[2]
    assert predictions[0] > series[-2][1] - 5  # roughly continues from the last fitted month


def test_band_is_ordered_and_non_negative():
    # 0 ≤ lower ≤ predicted ≤ upper for every point, including a steep decline that floors at 0.
    for counts in ([max(0, 80 - 2 * i) for i in range(40)], _seasonal(40, 12, 3, 10)):
        for point in forecast_series(_months("2022-01", counts)):
            assert 0 <= point["lower"] <= point["predicted"] <= point["upper"]


def test_band_widens_with_horizon():
    # Further-out points carry an honestly wider band (√h growth of the residual spread). The noise
    # cycles on a period coprime with 12 so the seasonal index can't absorb it — residual std stays
    # non-zero and the band has width to grow.
    counts = [12 + (10 if i % 12 == 2 else 0) + (i % 7 - 3) for i in range(40)]
    forecast = forecast_series(_months("2022-01", counts), horizon=3)
    widths = [p["upper"] - p["lower"] for p in forecast]
    assert widths[0] < widths[1] < widths[2]


def test_add_months_and_month_number():
    assert _add_months("2025-12", 1) == "2026-01"
    assert _add_months("2025-01", -1) == "2024-12"
    assert _add_months("2025-06", -12) == "2024-06"
    assert _add_months("2025-03", 0) == "2025-03"
    assert _month_number("2025-07") == 7
