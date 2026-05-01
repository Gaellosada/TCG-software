# Indicators Library

This document is the reference for the default indicators shipped with the
Indicators page (`frontend/src/pages/Indicators/defaults/*.js`). Every entry
is `readonly: true` in the UI — users cannot edit the source code, only the
parameter values and the instrument/label mapping per session.

## Library shape (post 2026-05 prune)

The library contains 9 entries:

- **Trend.** `sma`, `ema`.
- **Momentum.** `rsi`, `macd-line`, `macd-signal`, `macd-histogram`.
- **Volatility.** `historical-vol`.
- **Pattern.** `swing-pivots`.
- **Statistical.** `percentile-filtered-return`.

Indicators that previously shipped under a "legacy-port" tier (atr,
bollinger family, engulfment-{pattern,exit}, impetus, weighted-impetus,
centred-slope, slope-{acceleration,statistics}, trailing-extreme,
rolling-percentile-bands, absolute-mean) were dropped — see
`docs/design-decisions.md` for the rationale. Bollinger Bands were dropped
along with that tier; users who need them can ship their own custom
indicator (the SMA + sample stddev composition is a few lines).

`percentile-filtered-return` is intentionally retained even though
`rolling-percentile-bands` was dropped: it is not a band over the close
series but a rolling percentile of a *derived* mean-reversion stream
(`(close - SMA) / SMA`), which is a different signal and not trivially
expressible in a one-line sandbox cell.

## Output contract

Every default implements `compute(series, ...params) -> np.ndarray`. The
returned array must be 1-D with the same length as the input series, NaN
on warm-up / undefined bars.

---

## Trend

### sma — Simple Moving Average
- **Category.** Trend.
- **Formula.** `SMA_t = (1/window) * sum_{k=t-window+1}^{t} close_k`.
- **Params.** `window: int = 20`.
- **Use.** Baseline trend filter; cross with price or with another SMA of
  different length.
- **Worked example (window = 3).**
  ```
  close:   [10, 11, 12, 14, 13]
  out:     [NaN, NaN, 11.0, 12.333..., 13.0]
  ```

### ema — Exponential Moving Average
- **Category.** Trend.
- **Formula.** `alpha = 2/(window+1); EMA_t = alpha*close_t + (1-alpha)*EMA_{t-1}`
  with the seed `EMA_{window-1} = SMA(close[0..window-1])`.
- **Params.** `window: int = 20`.
- **Use.** Faster-reacting trend smoother than SMA for the same nominal
  window.
- **Worked example (window = 3).** alpha = 0.5.
  ```
  close:   [10, 11, 12, 14, 13]
  out:     [NaN, NaN, 11.0, 12.5, 12.75]
  ```

---

## Momentum

### rsi — Relative Strength Index
- **Category.** Momentum. Own-panel.
- **Formula.** Wilder smoothing of gains/losses → `RSI = 100 - 100/(1+RS)`.
- **Params.** `window: int = 14`.
- **Use.** Bounded momentum oscillator in `[0, 100]`; 70/30 are the
  traditional overbought/oversold thresholds.
- **Warm-up.** First `window` bars are NaN.

### macd-line — MACD Line
- **Category.** Momentum. Own-panel.
- **Formula.** `MACD = EMA(close, fast) - EMA(close, slow)`.
- **Params.** `fast: int = 12`, `slow: int = 26`.
- **Use.** Zero-crossings and slope changes are the canonical signals.

### macd-signal — MACD Signal
- **Category.** Momentum. Own-panel.
- **Formula.** `Signal = EMA(MACD, signal)`.
- **Params.** `fast: int = 12`, `slow: int = 26`, `signal: int = 9`.
- **Use.** Smoothed trigger; MACD/Signal crossovers are momentum signals.

### macd-histogram — MACD Histogram
- **Category.** Momentum. Own-panel.
- **Formula.** `Histogram = MACD - Signal`.
- **Params.** `fast: int = 12`, `slow: int = 26`, `signal: int = 9`.
- **Use.** Most actionable MACD view; sign flips correspond to crossovers.

---

## Volatility

### historical-vol — Historical Volatility
- **Category.** Volatility. Own-panel.
- **Formula.**
  ```
  ret_t  = close_t / close_{t-1} - 1
  hvol_t = std(ret_{t-window+1..t}, ddof=1) * sqrt(252) * 100
  ```
- **Params.** `window: int = 20`.
- **Use.** Realised-volatility estimator; regime gauge; benchmark for
  implied vol; volatility-targeted position sizing.
- **Note.** Uses simple percentage returns (not log returns). The
  difference is negligible for small daily moves and diverges for large
  ones.

---

## Pattern

### swing-pivots — Swing Pivots
- **Category.** Pattern. Overlay (rendered as markers).
- **Formula.** Confirmed local extrema with an `inflection_periods`-bar
  delay; see the indicator's `doc` field for the full state machine.
- **Params.** `total_periods: int = 20`, `inflection_periods: int = 5`.
- **Use.** Support / resistance; zig-zag / swing-high-low overlays.

---

## Statistical

### percentile-filtered-return — Percentile-Filtered Return
- **Category.** Statistical. Own-panel.
- **Formula.** `r_t = (close_t - SMA_t) / SMA_t`; rolling percentile of
  `r_t` over `window` bars.
- **Params.** `window: int = 252`, `filter_window: int = 50`,
  `percentile: float = 95.0`.
- **Use.** Mean-reversion triggers against a smoothed reference.
- **Composition.** The reference filter is an SMA of close by default.
  Users wanting an EMA/Kalman reference can pre-smooth in an upstream
  sandbox cell and feed that series in place of close.
