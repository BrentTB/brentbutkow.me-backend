# Recall anomaly detection — methodology

**Shipped detector (runtime):** robust z-score (median + MAD) over the monthly recall count, scoped
to overall volume, each cause category, and the busiest entities. A month is flagged when |z| ≥ 3
against its trailing 12-month baseline; the in-progress final month is excluded. Pure stdlib, so it
runs on every `/recalls/stats` call with no extra dependency.

**Why robust:** median + MAD resist a single past spike poisoning the baseline — a mean + stddev
would let one outlier mask the next.

**Offline validation — STL:** statsmodels seasonal-trend decomposition (LOESS, annual period)
splits the overall series into trend + seasonal + residual; anomalies are large residuals. It is
the stricter, seasonality-aware reference the cheap runtime detector is checked against — not
shipped, because it needs ≥ 2 seasonal cycles of history and a heavier dependency.

**Agreement (overall series, N=153 months):** z-score flags 14, STL flags 21; they agree on 11 (Jaccard 0.46).

**Honest limits:** a flag means "unusual vs recent history", never a forecast; category labels are
weakly supervised and entities come from a curated gazetteer.
