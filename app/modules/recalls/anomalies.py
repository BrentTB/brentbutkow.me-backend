"""Anomaly detection over monthly recall counts — robust z-score against a trailing baseline.

Honest by construction: flags a month whose count deviates sharply from its own recent history
(*detect*), and never forecasts (*predict*). Robust (median + MAD) so a single past spike doesn't
poison the baseline. Pure stdlib, so it runs on-read with no extra dependency. statsmodels STL is
used offline (scripts/anomaly_methodology.py) only to validate this detector against seasonality.
"""

import statistics
from typing import TypedDict

# Scales the median absolute deviation to a normal-consistent standard-deviation estimate.
_MAD_TO_SIGMA = 0.6745


class AnomalyPoint(TypedDict):
    month: str
    observed: int
    baseline: float
    z: float


def detect_anomalies(
    series: list[tuple[str, int]],
    *,
    window: int = 12,
    min_history: int = 6,
    threshold: float = 3.0,
) -> list[AnomalyPoint]:
    """Flag months whose |robust z-score| ≥ threshold vs the prior `window` months.

    `series` is chronological `(month, count)` with a continuous monthly index (gaps filled with 0).
    The final month is dropped — it is usually still in progress, so its partial count would read as
    a false drop (or spike). A month is scored only once it has ≥ `min_history` months behind it.
    """
    points = series[:-1]  # drop the in-progress final month
    out: list[AnomalyPoint] = []
    for index in range(min_history, len(points)):
        history = [count for _, count in points[max(0, index - window) : index]]
        observed = points[index][1]
        median = statistics.median(history)
        mad = statistics.median([abs(count - median) for count in history])
        if mad > 0:
            z = _MAD_TO_SIGMA * (observed - median) / mad
        else:
            # Degenerate flat baseline — fall back to stddev; if that's 0 too there's no scale to
            # measure against, so skip rather than report a meaningless infinity.
            sd = statistics.pstdev(history)
            if sd == 0:
                continue
            z = (observed - median) / sd
        if abs(z) >= threshold:
            out.append(
                {
                    "month": points[index][0],
                    "observed": observed,
                    "baseline": median,
                    "z": round(z, 1),
                }
            )
    return out
