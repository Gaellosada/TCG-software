# Indicators Library

This document is the reference for the default indicators shipped with the
Indicators page (`frontend/src/pages/Indicators/defaults/*.js`). Every entry
is `readonly: true` in the UI — users cannot edit the source code, only the
parameter values and the instrument/label mapping per session.

## Two-tier philosophy

The library splits into two tiers:

- **Canonical tier (5 logical indicators, 10 JS entries).** The universally
  known building blocks every technical-analysis platform provides: SMA,
  EMA, RSI, MACD (line + signal + histogram), Bollinger Bands (upper +
  middle + lower + %B). These are the default starting points; beginners
  and advanced users alike expect them. Bollinger is shipped as four files
  rather than a single multi-channel entry because the sandbox contract
  `compute(series, ...)` returns a single 1-D array — each band therefore
  needs its own entry so the UI can render all four as independent
  overlays. MACD is shipped as three files for the same reason (line,
  signal, histogram are three distinct plots).

- **Legacy-port tier (13 JS entries).** Hand-ported from an upstream
  reference implementation. These ports carry forward the original
  behaviour (quirks, tie-breakers) and — in two cases — deliberately
  correct latent inconsistencies or adapt the architecture to fit the
  scalar-per-bar sandbox contract. Each port's `doc` field flags any
  behavioural divergence users should be aware of.

Indicators from the reference set that did **not** make the cut as shipped
JS defaults are listed at the end of this document; each has a one-line
rationale.

## Output contract

Every default implements `compute(series, ...params) -> np.ndarray`. The
returned array must be 1-D with the same length as the input series, NaN
on warm-up / undefined bars. Multi-channel outputs from the reference
implementations are folded into a single primary scalar channel; where
direction is material (e.g. engulfment breakouts) it is encoded in the
sign of the emitted value.

---

## Canonical tier

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

### bollinger-upper — Bollinger Upper Band
- **Category.** Volatility. Overlay.
- **Formula.** `upper_t = SMA(close, window) + num_std * sqrt(var_t)` where
  `var_t = E[close^2] - E[close]^2` is the population variance over `window`.
- **Params.** `window: int = 20`, `num_std: float = 2.0`.
- **Use.** Upper statistical band; piercing it is an over-extension signal.

### bollinger-middle — Bollinger Middle Band
- **Category.** Volatility. Overlay.
- **Formula.** Same as SMA.
- **Params.** `window: int = 20`.
- **Use.** Middle band; shipped separately so the three-band overlay
  renders correctly.

### bollinger-lower — Bollinger Lower Band
- **Category.** Volatility. Overlay.
- **Formula.** `lower_t = SMA(close, window) - num_std * sqrt(var_t)`.
- **Params.** `window: int = 20`, `num_std: float = 2.0`.
- **Use.** Lower statistical band.

### bollinger-percent-b — Bollinger %B
- **Category.** Volatility. Own-panel.
- **Formula.** `%B = (close - lower) / (upper - lower)`.
- **Params.** `window: int = 20`, `num_std: float = 2.0`.
- **Use.** Normalised location in the band; >1 or <0 signals band break.
- **Edge case.** Flat series (`upper == lower`) → NaN.

---

## Legacy-port tier

All entries in this tier are ports of reference implementations from an
upstream simulator. Each entry's `doc` field in the UI describes the
user-facing behaviour; where behaviour diverges from the original, the
divergence is called out there.

### atr — Average True Range
- **Category.** Volatility. Own-panel.
- **Formula.**
  ```
  TR_t  = max(high_t - low_t, |high_t - close_{t-1}|, |close_{t-1} - low_t|)
  ATR_t = (1/window) * sum_{k=t-window+1}^{t} TR_k
  ```
- **Params.** `window: int = 14`.
- **Use.** Volatility proxy; stop sizing (`k * ATR`); normalising breakouts
  across instruments of different price levels.
- **Note.** Uses a simple **arithmetic mean** of TR, **not Wilder's
  recursive smoothing** used by the textbook ATR. Practical difference:
  reacts slightly faster to recent TR and has no infinite tail — each TR
  drops out after `window` bars.

### absolute-mean — Rolling Absolute Mean
- **Category.** Statistical. Overlay.
- **Formula.** `out_t = (1/window) * sum_{k=t-window+1}^{t} |close_k|`.
- **Params.** `window: int = 20`.
- **Use.** Rolling magnitude of a signed stream (averaging `|return|`,
  `|slope|`, etc.).
- **Note.** `abs` is applied consistently across both the initial window
  and incremental updates. Some reference implementations apply `abs` only
  at initialization, producing results that differ depending on warm-up
  path — this port fixes that inconsistency.

### engulfment-pattern — Engulfment Pattern
- **Category.** Pattern. Overlay.
- **Formula.** See the indicator's own `doc` field. State machine tracking
  a rolling consolidation box and emitting signed breakout levels
  (positive = upside, negative = downside).
- **Params.** `min_engulfing_periods: int = 5`.
- **Use.** Range-breakout entry signal after consolidations.
- **Output encoding.** Sign encodes direction; take `abs` for the price.

### engulfment-exit — Engulfment TP/SL Exit
- **Category.** Pattern. Overlay.
- **Use.** Paired exit monitor for `engulfment-pattern`. User pipes the
  `engulfment-pattern` signed output as `series['entry']`; this indicator
  watches for TP/SL hits and emits the fill price.
- **Params.** `box_lookback: int = 20`, `ratio_win: float = 2.0`,
  `ratio_loss: float = 1.0`.
- **Composition.** Entry parameters are supplied as an explicit `entry`
  input series (paired with `engulfment-pattern`) plus a `box_lookback`
  estimate, rather than via a side-channel.

### impetus — Impetus
- **Category.** Momentum. Own-panel.
- **Formula.** `sign_t = +1 if close_t >= close_{t-1} else -1;
  impetus_t = sum_{k=t-window+1}^{t} sign_k`.
- **Params.** `window: int = 14`.
- **Use.** Sign-only momentum; robust to magnitude outliers.

### trailing-extreme — Trailing Extreme
- **Category.** Pattern. Overlay.
- **Formula.** Rolling max or rolling min of close over `window`, selected
  by a boolean flag.
- **Params.** `window: int = 20`, `use_min: bool = False`.
- **Use.** Chandelier stops; breakout levels; envelope of recent action.

### swing-pivots — Swing Pivots
- **Category.** Pattern. Overlay (rendered as markers).
- **Formula.** Confirmed local extrema with an `inflection_periods`-bar
  delay; see the indicator's `doc` field for the full state machine.
- **Params.** `total_periods: int = 20`, `inflection_periods: int = 5`.
- **Use.** Support / resistance; zig-zag / swing-high-low overlays.
- **Naming correction.** Not a "Donchian Channel" — Donchian is rolling
  high / rolling low envelopes; this detects discrete pivots. Use
  `trailing-extreme` if you want rolling extreme bands.

### rolling-percentile-bands — Rolling Percentile Bands
- **Category.** Statistical. Overlay.
- **Formula.** Nearest-rank percentile of close over the last `window`
  bars: `out_t = sort(close[t-window+1..t])[rank]`.
- **Params.** `window: int = 252`, `rank: int = 95`.
- **Use.** Non-parametric threshold bands.
- **Note.** Emits a single `rank`-th order statistic. Practitioners
  wanting multiple bands instantiate the indicator multiple times.
- **Worked example (window = 5, rank = 3).** `rank` is a 0-indexed
  position into the ascending sorted window, so `rank = 3` returns the
  4th-lowest value in the 5-bar window (the 80th nearest-rank percentile).
  ```
  bar | close | sorted window (ascending)  | rolling-percentile-bands [rank=3]
   0  |  10   |  —                          |   NaN  (window not full)
   1  |  11   |  —                          |   NaN
   2  |  12   |  —                          |   NaN
   3  |  14   |  —                          |   NaN
   4  |  13   |  [10, 11, 12, 13, 14]       |  13.0   (index 3 → 4th-lowest = 13)
  ```
  If close on bar 4 were 16 instead of 13, the sorted window would be
  [10, 11, 12, 14, 16] and rank 3 would return 14 — output tracks the
  empirical distribution, not a fixed price level.

### percentile-filtered-return — Percentile-Filtered Return
- **Category.** Statistical. Own-panel.
- **Formula.** `r_t = (close_t - SMA_t) / SMA_t`; rolling percentile of
  `r_t` over `window` bars.
- **Params.** `window: int = 252`, `filter_window: int = 50`,
  `rank: int = 95`.
- **Use.** Mean-reversion triggers against a smoothed reference.
- **Composition.** The reference filter is an SMA of close by default.
  Users wanting an EMA/Kalman reference can pre-smooth in an upstream
  sandbox cell and feed that series in place of close.

### centred-slope — Centred Slope
- **Category.** Momentum. Own-panel.
- **Formula.** `slope_t = (close_t - close_{t-window}) /
  ((close_t + close_{t-window}) / 2)`.
- **Params.** `window: int = 1` (consecutive bars by default; exposed as optional).
- **Use.** Symmetric per-bar slope; second-order Taylor approximation of
  log-return.
- **Worked example (window = 1, default).** The denominator is the midpoint
  of the current and prior bar, making the result symmetric: if you swap
  `close_t` and `close_{t-1}` the value just changes sign.
  ```
  bar | close | centred-slope
   0  |  10   |  NaN
   1  |  11   |  0.09524   (= (11-10) / ((11+10)/2) = 1/10.5)
   2  |  12   |  0.08696   (= (12-11) / ((12+11)/2) = 1/11.5)
   3  |  14   |  0.15385   (= (14-12) / ((14+12)/2) = 2/13.0)
   4  |  13   | -0.07407   (= (13-14) / ((13+14)/2) = -1/13.5)
  ```
  Compare bar 3 (+0.154, +2 move from 12) and bar 4 (−0.074, −1 move from
  14): the denominator tracks the price level, so a move of the same
  absolute size yields a smaller ratio at higher prices — the midpoint
  normalisation gives the measure a log-return flavour without the `log`
  call.

### slope-acceleration — Slope Acceleration
- **Category.** Momentum. Own-panel.
- **Formula.** `accel_t = (close_t/close_{t-1} - 1) - (close_{t-1}/close_{t-2} - 1)`.
- **Params.** None.
- **Use.** Cheap second-derivative proxy; sign reveals acceleration /
  deceleration / reversal.

### slope-statistics — Slope Statistics
- **Category.** Statistical. Own-panel.
- **Formula.** Rolling sample stddev of simple returns:
  `stddev_t = sqrt((sumSq * n - sum^2) / (n*(n-1)))`.
- **Params.** `window: int = 20`.
- **Use.** Real-time estimator of return volatility for Sharpe / position
  sizing.
- **Note.** Surfaces the stddev channel only (the most useful summary).
  Users wanting the rolling mean of returns or the raw per-bar return can
  derive them in one line in a separate sandbox cell.

### weighted-impetus — Weighted Impetus
- **Category.** Momentum. Own-panel.
- **Formula.** `impetus_t = sum_{k=t-window+1}^{t} (close_k - close_{k-1}) =
  close_t - close_{t-window}` (telescoping).
- **Params.** `window: int = 14`.
- **Use.** Signed-magnitude momentum over a window.
- **Naming correction.** Despite the name "Weighted", there is **no
  volume weighting** in this indicator. The "weighting" refers to
  weighting each step by its signed magnitude (vs. the sign-only
  `impetus`). Volume-weighted variants are not shipped.
- **Note on volatility channel.** An auxiliary volatility channel using a
  non-standard correction term (based on the sum of absolute changes
  rather than the sum of signed changes) exists upstream but is not
  surfaced here; numbers would not match a textbook sample stddev of
  returns.
- **Worked example (window = 3).** `out[t] = close[t] - close[t-3]`;
  the first 3 bars are NaN.
  ```
  bar | close | weighted-impetus
   0  |  10   |   NaN
   1  |  11   |   NaN
   2  |  12   |   NaN
   3  |  14   |   4.0   (= 14 - 10)
   4  |  13   |   2.0   (= 13 - 11)
  ```
  Note how the telescoping sum preserves sign: the window moved from
  [10,11,12] to [11,12,13], but the signed displacement shrinks from +4 to
  +2 despite the close dropping only one unit — the companion
  stddev-of-deltas channel (not surfaced here) would have captured that
  within-window volatility.

---

## Not shipped (documented-only)

Four indicators from the reference set are intentionally **not** ported as
shipped defaults. Per-class rationale:

- **Identity / passthrough** — not a practitioner-facing indicator; an
  adapter primitive in the upstream two-stage architecture. No value as a
  standalone default.

- **Scalar-constant transform** (add / sub / mul / div by a constant) —
  does not fit the sandbox contract (which takes raw series only, not
  upstream indicators), and is trivially expressed inline in any user
  sandbox cell (`out = indicator + k`).

- **Single-class Bollinger Bands** — redundant with the canonical 4-file
  Bollinger bundle shipped in the canonical tier. The 4-file form is
  required anyway (one 1-D series per `compute` call) so a single-class
  port would only add maintenance cost.

- **Filter history buffer** — emits the entire rolling window of values
  as an array per bar. Does not fit the scalar-per-bar contract
  (`compute` returns a 1-D array aligned to the input length). It is an
  infrastructure primitive, not an end-user indicator.
