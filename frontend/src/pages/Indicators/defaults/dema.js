// Double Exponential Moving Average — 2*EMA(s) - EMA(EMA(s)).
// Reduces the lag of a plain EMA while preserving its smoothing profile.
// First valid output at index 2*(window-1).
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < 2 * window - 1:
        return out
    alpha = 2.0 / (window + 1)
    ema1 = np.full(n, np.nan, dtype=float)
    seed1 = np.mean(s[:window])
    ema1[window-1] = seed1
    prev = seed1
    for i in range(window, n):
        prev = alpha * s[i] + (1 - alpha) * prev
        ema1[i] = prev
    # EMA of EMA — starts at ema1[window-1], seed is the mean of the next
    # 'window' values of ema1.
    ema2 = np.full(n, np.nan, dtype=float)
    start = window - 1
    seed2_end = start + window
    if seed2_end > n:
        return out
    seed2 = np.mean(ema1[start:seed2_end])
    ema2[seed2_end - 1] = seed2
    prev = seed2
    for i in range(seed2_end, n):
        prev = alpha * ema1[i] + (1 - alpha) * prev
        ema2[i] = prev
    out = 2 * ema1 - ema2
    return out`;

export default {
  id: 'dema',
  name: 'DEMA',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Double Exponential Moving Average — reduces EMA lag via \`2·EMA(close) − EMA(EMA(close))\`. First valid output is at index \`2·(window − 1)\` of the closing-price series.

**Parameters**
- \`window\`: EMA span. Smaller window → tighter tracking but more noise; note warmup doubles vs plain EMA.`,
};
