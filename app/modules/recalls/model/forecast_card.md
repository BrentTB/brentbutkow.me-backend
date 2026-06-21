# Recall volume forecast — methodology

**Shipped forecaster (runtime):** a self-built additive seasonal model over the overall monthly
recall count — a 12-month seasonal index plus a linear level/slope on the deseasonalised series,
projected 3 months ahead with a ~1σ band that widens with the horizon. Pure numpy, so it
runs on every `/recalls/stats` call with no extra dependency. The in-progress final month is dropped
before fitting, exactly as the anomaly detector does.

**Offline reference — Holt-Winters:** statsmodels Exponential Smoothing (additive trend + seasonal,
annual period) — a mature, seasonality-aware forecaster. It needs ≥ 2 seasonal cycles and a heavier
dependency, so it is the reference the cheap runtime model is checked against, not shipped.

**Backtest (overall series, N=153 months, last 3 held out):** mean absolute error —
shipped 48.87, Holt-Winters 48.85, naive same-month-last-year 40.00. The shipped model
matches Holt-Winters and trails the naive baseline on this split.

**Honest limits:** a forecast is a projection of recent trend + typical seasonal lift, never a
promise; the band is the recent in-sample error, not a guarantee. Short or sparse series return no
forecast at all rather than a confident-looking bad line.
