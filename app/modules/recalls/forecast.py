"""Short-horizon forecast of monthly recall volume — a pure-numpy seasonal model, computed on read.

The counterpart to anomalies.py. Where the detector says what *already* spiked (*detect, never
predict*), this projects what's *coming*: the next few months of overall recall volume, each with a
confidence band. Same honesty constraints as the rest of Recall Radar — deterministic, explainable,
no LLM and no pretrained model, just arithmetic over the series.

The model is a **multiplicative** seasonal decomposition cheap enough for the request path. We fit
in log space (``log1p``), so an additive seasonal-plus-trend fit there is multiplicative back in
counts — a seasonal lift scales with the level (a busy December adds a *fraction* more, not a fixed
count), which fits volume that has grown several-fold far better than a fixed offset (a rolling
backtest picked this over the additive form). In log space:

* a **12-month seasonal index** — the average log-deviation of each calendar month from the local
  trend, so a reliably busy March lifts every projected March; and
* a **linear level + slope** fit by least squares on the deseasonalised series.

A forecast is ``expm1(level + slope·t + seasonal[month])``, floored at zero. The band comes from the
in-sample residual spread *in counts* (std of actual − fitted) and widens with the horizon (``√h``),
so a three-months-out point is honestly less certain than next month's. statsmodels Holt-Winters is
used offline (scripts/forecast_methodology.py) only to validate this self-built forecaster — it is
never imported on the request path, exactly as STL stays offline for the anomaly detector.
"""

from typing import TypedDict

import numpy as np

# A 12-month seasonal index needs ≥ 2 full cycles to be stable; with fewer we fall back to a
# trend-only forecast (seasonal component left at zero) rather than fitting noise into the calendar.
_SEASONAL_MIN_CYCLES = 2

# Band half-width in residual-sigma units — a ~1σ "typical forecast error" envelope, not a formal
# confidence interval. Honest and modest; it still widens with the horizon (see forecast_series).
_BAND_Z = 1.0


class ForecastPoint(TypedDict):
    month: str
    predicted: float
    lower: float
    upper: float


def forecast_series(
    series: list[tuple[str, int]],
    *,
    horizon: int = 3,
    period: int = 12,
    min_history: int = 24,
) -> list[ForecastPoint]:
    """Project the next `horizon` months of `series` with a seasonal-plus-trend model.

    `series` is chronological `(month, count)` on a continuous monthly index (gaps filled with 0) —
    the same shape the anomaly detector consumes. The in-progress final month is dropped before
    fitting, like `detect_anomalies`, so its partial count can't drag the level or slope; the
    horizon then covers that current month forward (its first point re-projects the partial month
    as a full one).

    Returns `[]` when there is less than `min_history` of history: a short series can't support a
    stable forecast, and an empty list reads as "no projection" to every caller (no overlay, no
    callout). Otherwise returns `horizon` points, each a `predicted` count with a `[lower, upper]`
    band, all floored at zero.
    """
    points = series[:-1]  # drop the in-progress final month, as detect_anomalies does
    if len(points) < min_history:
        return []

    counts = np.array([count for _, count in points], dtype=float)
    months = [month for month, _ in points]
    n = len(counts)
    t = np.arange(n, dtype=float)
    calendar = np.array([_month_number(month) - 1 for month in months])  # 0..11
    # Fit in log space so seasonality is multiplicative (scales with the level), not a fixed offset.
    log_counts = np.log1p(counts)

    # Seasonal index: detrend with a rough line, average the residual per calendar month, then
    # centre it so it's a pure offset (sums to ~0) that doesn't double-count the level/trend.
    seasonal = np.zeros(period)
    if n >= _SEASONAL_MIN_CYCLES * period:
        rough = np.polyfit(t, log_counts, 1)
        detrended = log_counts - (rough[1] + rough[0] * t)
        for cal in range(period):
            month_values = detrended[calendar == cal]
            if month_values.size:
                seasonal[cal] = month_values.mean()
        seasonal -= seasonal.mean()

    # Level + slope on the deseasonalised (log) series. Reconstruct fitted values back in counts
    # (expm1) so the residual spread — and therefore the band — stays in recalls/month.
    deseasonalised = log_counts - seasonal[calendar]
    fit = np.polyfit(t, deseasonalised, 1)
    slope, level = float(fit[0]), float(fit[1])
    fitted = np.expm1(level + slope * t + seasonal[calendar])
    sigma = float(np.std(counts - fitted, ddof=1)) if n > 2 else float(np.std(counts - fitted))

    out: list[ForecastPoint] = []
    last_month = months[-1]
    for step in range(1, horizon + 1):
        future_month = _add_months(last_month, step)
        future_t = (n - 1) + step
        cal = _month_number(future_month) - 1
        predicted = max(0.0, float(np.expm1(level + slope * future_t + float(seasonal[cal]))))
        band = _BAND_Z * sigma * (step**0.5)  # honest widening: further out, less certain
        out.append(
            {
                "month": future_month,
                "predicted": round(predicted, 1),
                "lower": round(max(0.0, predicted - band), 1),
                "upper": round(predicted + band, 1),
            }
        )
    return out


def _month_number(month: str) -> int:
    """Calendar month 1..12 from a `YYYY-MM` label."""
    return int(month[5:7])


def _add_months(month: str, n: int) -> str:
    """`YYYY-MM` exactly `n` months after `month` (n ≥ 0)."""
    total = (int(month[:4]) * 12 + int(month[5:7]) - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"
