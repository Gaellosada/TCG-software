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
  category: 'momentum',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** The MACD Line is the difference between a fast and a slow EMA of close. When fast EMA > slow EMA the line is positive and price momentum is upward; when fast < slow the line is negative. Zero-crossings and turns in slope are the primary signals. It is the first of three related plots (line, signal, histogram) that together form the MACD system.

**Formula.**
\`\`\`
EMA_fast_t = EMA(close, fast)_t
EMA_slow_t = EMA(close, slow)_t
MACD_t     = EMA_fast_t - EMA_slow_t
\`\`\`
Both EMAs are seeded with their first-\`n\`-bar SMA.

**Parameters**
- \`fast\` (int, default 12): span of the fast EMA. Smaller values react more quickly to price.
- \`slow\` (int, default 26): span of the slow EMA. Must exceed \`fast\`. Controls the trend baseline.

**Edge cases**
- Output is \`NaN\` for the first \`slow - 1\` bars (warm-up — the slow EMA controls the start index).
- If \`n < slow\` the output is all \`NaN\`.
- If \`fast >= slow\` the indicator still computes but the interpretation inverts; users should ensure \`fast < slow\`.
- \`NaN\` in the input before the seed bar contaminates \`np.mean\` and propagates downstream.`,
  ownPanel: true,
};
