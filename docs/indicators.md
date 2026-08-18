# Indicators Library

This document is the reference for the default indicators shipped with the
Indicators page (`frontend/src/pages/Indicators/defaults/*.js`). Every entry
is `readonly: true` in the UI — users cannot edit the source code, only the
parameter values and the instrument/label mapping per session.

## Library shape (post 2026-05 prune)

The library contains 13 entries (9 core + `atm-contract-iv` /
`term-structure-slope` from the options wave + `dstat` / `dstat-percentile`
from the DStat wave):

- **Trend.** `sma`, `ema`.
- **Momentum.** `rsi`, `macd-line`, `macd-signal`, `macd-histogram`.
- **Volatility.** `historical-vol`, `atm-contract-iv`, `term-structure-slope`.
- **Pattern.** `swing-pivots`.
- **Statistical.** `percentile-filtered-return`, `dstat`, `dstat-percentile`.

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
- **MA5 / MA20 / MA50 / MA200 presets.** The strategy layer's named moving
  averages (SPEC §4.3: `MA5(VIX)`, `MA20(SPX)`, `MA50(VVIX)`, `MA200(SPX)`)
  are this exact `sma` indicator with `window = 5 / 20 / 50 / 200`. No
  separate default files are shipped — instantiate `sma` with the required
  `window`.

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
- **HV20 / HV30 / HV100 presets.** The strategy layer's `HV20`, `HV30`,
  `HV100` (SPEC §4.2) are this exact indicator instantiated with
  `window = 20 / 30 / 100`. The formula matches SPEC §4.2 verbatim (simple
  returns, `n-1` denominator, `sqrt(252)` annualisation) — no separate
  default file is shipped; instantiate `historical-vol` with the required
  `window` instead. (The `* 100` percent scaling here is display-only and
  does not affect HVOL-regime comparisons, which are between HV lines.)

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

### dstat — DStat (raw) — SPEC §4.1 Layer 1
- **Category.** Statistical. Own-panel.
- **Formula.**
  ```
  r_t     = ln(close_t / close_{t-1})                      (log returns)
  MA_t    = (1/ma_window) * sum_{k=t-ma_window+1..t} close_k
  vol_t   = std(r_{t-vol_window+1..t}, ddof=1) * sqrt(252) (annualised sample stdev)
  DSTAT_t = (close_t / MA_t - 1) / vol_t
  ```
- **Params.** `ma_window: int = 21`, `vol_window: int = 63`.
- **Use.** Vol-normalised distance of price from its moving average — the
  Layer-1 statistic of the canonical legacy DStat regime engine (same
  parameters on SPX, VIX, VVIX).
- **Warm-up.** First value at bar `max(ma_window, 2*vol_window)` (= 126 for
  defaults). This matches the legacy engine and is deliberately more
  conservative than the strict minimum the formula needs
  (`max(ma_window-1, vol_window)`); see `PROBLEMS.md`.
- **Note.** Uses **log** returns for the vol term (SPEC §4.1 canonical
  variant), unlike `historical-vol` which uses simple returns. A flat vol
  window (`vol_t = 0`) leaves the bar `NaN` rather than `±inf`.

### dstat-percentile — DStat Percentile line — SPEC §4.1 Layer 2
- **Category.** Statistical. Own-panel.
- **Formula.** Recomputes raw DStat internally (self-contained), then emits
  the nearest-rank percentile over the trailing `pct_window` raw values:
  ```
  raw_t = DStat(close; ma_window, vol_window)
  idx   = ceil((percentile/100) * pct_window) - 1        (0-based, clamped to [0, pct_window-1])
  out_t = sort(raw_{t-pct_window+1..t})[idx]             (nearest-rank; observed, not interpolated)
  ```
- **Params.** `ma_window: int = 21`, `vol_window: int = 63`,
  `pct_window: int = 1260`, `percentile: float = 95.0` (0..100 scale).
- **Use.** The trailing reference line for DStat regime rules — "DSTAT hits
  95%" ≡ raw DStat rising above this line with `percentile = 95`.
  Instantiate copies at 10/20/50/60/75/95 for the strategies' hysteresis
  bands.
- **Warm-up.** `NaN` until a full window exists: first value at
  `max(ma_window, 2*vol_window) + pct_window - 1` (= 1385 for defaults).
  `percentile` outside `[0, 100]` → all `NaN`.
