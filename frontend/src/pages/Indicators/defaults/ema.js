// Exponential Moving Average — recursive filter with alpha = 2 / (window + 1).
// Seeded with the SMA of the first ``window`` values so the recursion has a
// stable anchor; outputs before index ``window-1`` are NaN.
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    alpha = 2.0 / (window + 1)
    seed = np.mean(s[:window])
    out[window-1] = seed
    prev = seed
    for i in range(window, n):
        prev = alpha * s[i] + (1 - alpha) * prev
        out[i] = prev
    return out`;

export default {
  id: 'ema',
  name: 'EMA',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
};
