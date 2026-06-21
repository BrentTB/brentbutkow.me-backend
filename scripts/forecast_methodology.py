"""Validate the shipped seasonal forecaster against statsmodels Holt-Winters, offline.

The runtime forecaster (app/modules/recalls/forecast.py) is a cheap, dependency-light multiplicative
seasonal model fit in log space (numpy only). This script fits statsmodels' Exponential Smoothing
(Holt-Winters, additive trend + seasonal) on the same overall monthly series, backtests both over
a held-out tail
against a naive same-month-last-year baseline, and writes a methodology card next to the
classifier's model card. Run with the `ml` extra installed:

    pip install -e '.[ml]'
    python -m scripts.forecast_methodology
"""

from sqlalchemy import func, select
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from app.db import SessionLocal
from app.modules.recalls.classifier import MODEL_PATH
from app.modules.recalls.forecast import _add_months, forecast_series
from app.modules.recalls.models import Recall
from app.modules.recalls.service import _continuous_months

_PERIOD = 12
_HORIZON = 3  # months held out for the backtest (and the shipped forecaster's horizon)
_MIN_HISTORY = 24


def _overall_series() -> list[tuple[str, int]]:
    session = SessionLocal()
    try:
        month = func.to_char(Recall.report_date, "YYYY-MM")
        rows = session.execute(
            select(month, func.count())
            .where(Recall.report_date.is_not(None))
            .group_by(month)
            .order_by(month)
        ).all()
    finally:
        session.close()
    counts = {m: c for m, c in rows}
    return [(m, counts.get(m, 0)) for m in _continuous_months([m for m, _ in rows])]


def _mae(predicted: list[float], actual: list[float]) -> float:
    return sum(abs(p - a) for p, a in zip(predicted, actual, strict=True)) / len(actual)


def _ours(points: list[tuple[str, int]]) -> list[float]:
    # Forecast the held-out tail with the shipped model: feed it the prefix ending at the first
    # held-out month (which it drops as "in progress"), so its horizon lands on the holdout exactly.
    prefix = points[: len(points) - _HORIZON + 1]
    return [p["predicted"] for p in forecast_series(prefix, horizon=_HORIZON)]


def _holt_winters(points: list[tuple[str, int]]) -> list[float]:
    # Additive Holt-Winters (trend + annual seasonal) — the seasonality-aware reference the cheap
    # numpy forecaster is checked against; too heavy for the request path.
    train = [float(c) for _, c in points[:-_HORIZON]]
    model = ExponentialSmoothing(train, trend="add", seasonal="add", seasonal_periods=_PERIOD).fit()
    return [float(value) for value in model.forecast(_HORIZON)]


def _naive(points: list[tuple[str, int]]) -> list[float]:
    # The dumb floor: predict each held-out month as its value one year earlier. The history gate in
    # main() guarantees ≥ 12 months precede every held-out month, so the `0` default never engages —
    # it's a guard against a real prior-year value being absent, not a silent zero baseline.
    index = {m: c for m, c in points}
    return [float(index.get(_add_months(m, -_PERIOD), 0)) for m, _ in points[-_HORIZON:]]


def _verdict(ours: float, other: float) -> str:
    # Within 5% of the reference's error reads as a tie — a 0.02-MAE gap isn't "worse".
    if abs(ours - other) <= 0.05 * max(other, 1.0):
        return "matches"
    return "beats" if ours < other else "trails"


def _write_card(n_months: int, ours: float, hw: float, naive: float) -> None:
    verdict = _verdict(ours, hw)
    floor = _verdict(ours, naive)
    card = f"""# Recall volume forecast — methodology

**Shipped forecaster (runtime):** a self-built multiplicative seasonal model over the overall
monthly recall count — a 12-month seasonal index plus a linear level/slope, fit in log space so a
seasonal swing scales with the level. Projected {_HORIZON} months ahead with a ~1σ band that widens
with the horizon. Pure numpy, so it runs on every `/recalls/stats` call with no extra dependency.
The in-progress final month is dropped before fitting, exactly as the anomaly detector does.

**Offline reference — Holt-Winters:** statsmodels Exponential Smoothing (additive trend + seasonal,
annual period) — a mature, seasonality-aware forecaster. It needs ≥ 2 seasonal cycles and a heavier
dependency, so it is the reference the cheap runtime model is checked against, not shipped.

**Backtest (overall series, N={n_months} months, last {_HORIZON} held out):** mean absolute error —
shipped {ours:.2f}, Holt-Winters {hw:.2f}, naive same-month-last-year {naive:.2f}. The shipped model
{verdict} Holt-Winters and {floor} the naive baseline on this split.

**Honest limits:** a forecast is a projection of recent trend + typical seasonal lift, never a
promise; the band is the recent in-sample error, not a guarantee. Short or sparse series return no
forecast at all rather than a confident-looking bad line.
"""
    (MODEL_PATH.parent / "forecast_card.md").write_text(card)


def main() -> None:
    series = _overall_series()
    # Two binding constraints on history: the runtime forecaster needs `_MIN_HISTORY` months after
    # its in-progress drop, and additive Holt-Winters needs strictly more than 2 seasonal cycles of
    # *training* data (`points[:-_HORIZON]` must exceed `2·_PERIOD`, else statsmodels raises). Gate
    # on whichever is larger so the offline fit never lands exactly on the 2-cycle boundary.
    need = max(_MIN_HISTORY, 2 * _PERIOD + 1) + _HORIZON + 1
    if len(series) < need:
        print(f"Not enough history to backtest ({len(series)} months; need ≥ {need}).")
        return
    points = series[:-1]  # drop the in-progress final month, as the runtime forecaster does
    actual = [float(c) for _, c in points[-_HORIZON:]]
    ours, hw, naive = _ours(points), _holt_winters(points), _naive(points)
    mae_ours, mae_hw, mae_naive = _mae(ours, actual), _mae(hw, actual), _mae(naive, actual)
    _write_card(len(series), mae_ours, mae_hw, mae_naive)
    print(f"held-out actuals: {[round(a, 1) for a in actual]}")
    print(f"shipped forecast: {[round(p, 1) for p in ours]} (MAE {mae_ours:.2f})")
    print(f"Holt-Winters:     {[round(p, 1) for p in hw]} (MAE {mae_hw:.2f})")
    print(f"naive seasonal:   {[round(p, 1) for p in naive]} (MAE {mae_naive:.2f})")
    print(f"shipped next {_HORIZON}: {forecast_series(series, horizon=_HORIZON)}")
    print(f"Wrote {MODEL_PATH.parent / 'forecast_card.md'}")


if __name__ == "__main__":
    main()
