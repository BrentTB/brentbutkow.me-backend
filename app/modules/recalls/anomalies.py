"""Anomaly detection over monthly recall counts — a robust z-score baseline plus a near-record rule.

Honest by construction: flags a month that either spikes sharply above its own recent history or
ranks among the largest the series has ever seen (*detect*), and never forecasts (*predict*).

The relative test is a **one-sided robust z-score** (median + MAD) against a trailing window. Three
guards keep it sane on the many near-zero categories, where a plain z-score would turn a 0→1 blip
into a giant score: it is one-sided (dips never flag), it **floors the baseline spread** (a
near-flat history can't yield a microscopic denominator), and it **floors the absolute rise** (a
month must clear the baseline by real recalls, not a large fraction of nearly nothing).

But the relative test alone misses genuinely large months that land in an already-choppy stretch —
their local baseline is so volatile that even a record-high month scores under 3σ. So a
**near-record rule** also flags any month whose count sits in the top `_HIGH_WATER_PCT` percent of
everything seen up to that point (look-back only, never the future), provided it still clears the
absolute-rise floor (so a flat high plateau doesn't light up end to end).

Pure stdlib, so it runs on-read with no extra dependency. statsmodels STL is used offline
(scripts/anomaly_methodology.py) only to validate this detector against seasonality.
"""

import statistics
from typing import TypedDict

# Scales the median absolute deviation to a normal-consistent standard-deviation estimate:
# sigma ≈ MAD / 0.6745.
_MAD_TO_SIGMA = 0.6745

# Floor on the baseline's spread estimate, in recalls/month. Stops a near-flat history — the norm
# for rare categories like fish or egg, where the window is mostly zeros and MAD collapses to 0 —
# from producing a microscopic denominator that turns a 0→1 blip into a huge z. In effect: "we
# can't claim the baseline is tighter than ±1 recall/month."
_MIN_SCALE = 1.0

# Floor on how far above baseline a month must sit, in recalls, before it can flag at all —
# independent of the z-score. Kills statistically-large-but-tiny moves (a 0→2 bump on a category
# that is otherwise always 0), and stops a high-but-flat plateau from flagging via the near-record
# rule (every month equals the record, but none rises above the baseline).
_MIN_ABSOLUTE_RISE = 3.0

# A month also flags if its count lands in the top (100 - _HIGH_WATER_PCT) percent of everything
# seen so far — the near-record rule that catches genuinely large months a volatile local baseline
# would otherwise wave through. Calibrated against the real UK series (see git history).
_HIGH_WATER_PCT = 90


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
    """Flag months that spike above the prior `window`, or rank among the series' highest ever.

    `series` is chronological `(month, count)` with a continuous monthly index (gaps filled with 0).
    The final month is dropped — it is usually still in progress, so its partial count would read as
    a false drop (or spike). A month is scored only once it has ≥ `min_history` months behind it.

    A month flags when it clears `_MIN_ABSOLUTE_RISE` above its trailing median AND either spikes to
    a robust z-score ≥ `threshold` (the relative test) or sits in the top `_HIGH_WATER_PCT` percent
    of everything seen so far (the near-record rule). See the module docstring for the why.
    """
    points = series[:-1]  # drop the in-progress final month
    out: list[AnomalyPoint] = []
    for index in range(min_history, len(points)):
        history = [count for _, count in points[max(0, index - window) : index]]
        observed = points[index][1]
        median = statistics.median(history)
        # Robust spread (median + MAD), falling back to stddev when MAD degenerates to 0 — which it
        # does whenever ≥ half the window shares a value, i.e. almost always for sparse categories.
        # Floor it either way so a near-flat baseline can't manufacture a giant z from a tiny move.
        mad = statistics.median([abs(count - median) for count in history])
        spread = mad / _MAD_TO_SIGMA if mad > 0 else statistics.pstdev(history)
        scale = max(spread, _MIN_SCALE)
        rise = observed - median
        z = rise / scale
        prior = [count for _, count in points[:index]]
        relative_spike = z >= threshold
        # Near-record needs a stable percentile, so wait for a full window of history behind us.
        near_record = len(prior) >= window and observed >= _high_water(prior)
        if (relative_spike or near_record) and rise >= _MIN_ABSOLUTE_RISE:
            out.append(
                {
                    "month": points[index][0],
                    "observed": observed,
                    "baseline": median,
                    "z": round(z, 1),
                }
            )
    return out


def _high_water(values: list[int]) -> float:
    """The `_HIGH_WATER_PCT`-th percentile of `values` — the bar a near-record month must clear.

    `statistics.quantiles` needs ≥ 2 points; the caller already gates on a full window of history.
    """
    return statistics.quantiles(values, n=100, method="inclusive")[_HIGH_WATER_PCT - 1]
