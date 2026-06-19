"""Validate the shipped robust-z anomaly detector against statsmodels STL, offline.

The runtime detector (app/modules/recalls/anomalies.py) is a cheap, dependency-free robust z-score.
This script decomposes the same overall monthly series with STL (seasonal-trend via LOESS) — which
is seasonality-aware but too heavy for the request path — and reports how the two agree, writing a
methodology card next to the classifier's model_card.md. Run with the `ml` extra installed:

    pip install -e '.[ml]'
    python -m scripts.anomaly_methodology
"""

import statistics

from sqlalchemy import func, select
from statsmodels.tsa.seasonal import STL

from app.db import SessionLocal
from app.modules.recalls.anomalies import _MAD_TO_SIGMA, detect_anomalies
from app.modules.recalls.classifier import MODEL_PATH
from app.modules.recalls.models import Recall
from app.modules.recalls.service import _continuous_months

_PERIOD = 12  # annual seasonality
_THRESHOLD = 3.0


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


def _stl_flags(series: list[tuple[str, int]]) -> set[str]:
    # Flag months whose STL residual is a high robust-z outlier. One-sided (positive residuals
    # only), matching the runtime detector — dips never flag — so the two agree on the same
    # criterion. Drop the partial final month, as the detector does, to compare on the same points.
    points = series[:-1]
    values = [float(count) for _, count in points]
    resid = list(STL(values, period=_PERIOD, robust=True).fit().resid)
    median = statistics.median(resid)
    mad = statistics.median([abs(r - median) for r in resid]) or 1.0
    return {
        points[i][0]
        for i, r in enumerate(resid)
        if _MAD_TO_SIGMA * (r - median) / mad >= _THRESHOLD
    }


def _write_card(n_months: int, zscore: set[str], stl: set[str]) -> None:
    overlap = zscore & stl
    union = zscore | stl
    jaccard = len(overlap) / len(union) if union else 1.0
    agreement = (
        f"z-score flags {len(zscore)}, STL flags {len(stl)}; "
        f"they agree on {len(overlap)} (Jaccard {jaccard:.2f})"
    )
    card = f"""# Recall anomaly detection — methodology

**Shipped detector (runtime):** robust z-score (median + MAD) over the monthly recall count, scoped
to overall volume, each cause category, and the busiest entities. A month is flagged when z ≥ 3
(one-sided) against its trailing 12-month baseline; the in-progress final month is excluded. Pure
stdlib, so it runs on every `/recalls/stats` call with no extra dependency.

**Why robust:** median + MAD resist a single past spike poisoning the baseline — a mean + stddev
would let one outlier mask the next.

**Offline validation — STL:** statsmodels seasonal-trend decomposition (LOESS, annual period)
splits the overall series into trend + seasonal + residual; anomalies are large residuals. It is
the stricter, seasonality-aware reference the cheap runtime detector is checked against — not
shipped, because it needs ≥ 2 seasonal cycles of history and a heavier dependency.

**Agreement (overall series, N={n_months} months):** {agreement}.

**Honest limits:** a flag means "unusual vs recent history", never a forecast; category labels are
weakly supervised and entities come from a curated gazetteer.
"""
    (MODEL_PATH.parent / "anomaly_card.md").write_text(card)


def main() -> None:
    series = _overall_series()
    if len(series) < 2 * _PERIOD + 1:
        print(f"Not enough history for STL ({len(series)} months; need ≥ {2 * _PERIOD + 1}).")
        return
    zscore = {point["month"] for point in detect_anomalies(series)}
    stl = _stl_flags(series)
    _write_card(len(series), zscore, stl)
    print(f"z-score flags: {sorted(zscore)}")
    print(f"STL flags:     {sorted(stl)}")
    print(f"Wrote {MODEL_PATH.parent / 'anomaly_card.md'}")


if __name__ == "__main__":
    main()
