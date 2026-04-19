// Triple Exponential Moving Average — 3*EMA - 3*EMA(EMA) + EMA(EMA(EMA)).
// Further reduces lag vs DEMA. First valid output at index 3*(window-1).
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < 3 * window - 2:
        return out
    alpha = 2.0 / (window + 1)
    # EMA1
    ema1 = np.full(n, np.nan, dtype=float)
    seed1 = np.mean(s[:window])
    ema1[window-1] = seed1
    prev = seed1
    for i in range(window, n):
        prev = alpha * s[i] + (1 - alpha) * prev
        ema1[i] = prev
    # EMA2 = EMA(EMA1)
    ema2 = np.full(n, np.nan, dtype=float)
    start2 = window - 1
    end2 = start2 + window
    seed2 = np.mean(ema1[start2:end2])
    ema2[end2 - 1] = seed2
    prev = seed2
    for i in range(end2, n):
        prev = alpha * ema1[i] + (1 - alpha) * prev
        ema2[i] = prev
    # EMA3 = EMA(EMA2)
    ema3 = np.full(n, np.nan, dtype=float)
    start3 = end2 - 1
    end3 = start3 + window
    seed3 = np.mean(ema2[start3:end3])
    ema3[end3 - 1] = seed3
    prev = seed3
    for i in range(end3, n):
        prev = alpha * ema2[i] + (1 - alpha) * prev
        ema3[i] = prev
    out = 3 * ema1 - 3 * ema2 + ema3
    return out`;

export default {
  id: 'tema',
  name: 'TEMA',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Triple Exponential Moving Average — further reduces lag via \`3·EMA − 3·EMA(EMA) + EMA(EMA(EMA))\` over closing prices. First valid output is at index \`3·(window − 1)\`.

**Parameters**
- \`window\`: EMA span applied at each of the three levels. Warmup is three times that of a plain EMA.`,
  ownPanel: false,
};
