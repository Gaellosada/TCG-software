// MACD line — EMA(close, fast) - EMA(close, slow).
// Each EMA is seeded with the SMA of its first ``n`` values.
const code = `def compute(series, fast: int = 12, slow: int = 26):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < slow:
        return out
    alpha_f = 2.0 / (fast + 1)
    alpha_s = 2.0 / (slow + 1)
    ema_f = np.full(n, np.nan, dtype=float)
    ema_s = np.full(n, np.nan, dtype=float)
    seed_f = np.mean(s[:fast])
    ema_f[fast-1] = seed_f
    prev = seed_f
    for i in range(fast, n):
        prev = alpha_f * s[i] + (1 - alpha_f) * prev
        ema_f[i] = prev
    seed_s = np.mean(s[:slow])
    ema_s[slow-1] = seed_s
    prev = seed_s
    for i in range(slow, n):
        prev = alpha_s * s[i] + (1 - alpha_s) * prev
        ema_s[i] = prev
    out = ema_f - ema_s
    return out`;

export default {
  id: 'macd-line',
  name: 'MACD Line',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `MACD Line — difference between a fast and slow EMA of closing prices: \`fast_EMA(close) − slow_EMA(close)\`. Each EMA is seeded with its first-\`n\`-bar SMA.

**Parameters**
- \`fast\`: span of the fast EMA. Typical value 12. Smaller values react more quickly to price.
- \`slow\`: span of the slow EMA. Must exceed \`fast\`. Typical value 26. Controls the trend baseline.`,
  ownPanel: true,
};
