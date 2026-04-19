// MACD signal line — EMA of the MACD line over ``signal`` bars.
// Computes the MACD line inline, then runs a signal-period EMA on top,
// seeded with the mean of the first ``signal`` valid MACD values.
const code = `def compute(series, fast: int = 12, slow: int = 26, signal: int = 9):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < slow + signal - 1:
        return out
    alpha_f = 2.0 / (fast + 1)
    alpha_s = 2.0 / (slow + 1)
    alpha_sig = 2.0 / (signal + 1)
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
    macd = ema_f - ema_s
    # Signal EMA seeded at slow-1 (first valid macd index) + signal - 1.
    start = slow - 1
    end = start + signal
    if end > n:
        return out
    seed_sig = np.mean(macd[start:end])
    out[end - 1] = seed_sig
    prev = seed_sig
    for i in range(end, n):
        prev = alpha_sig * macd[i] + (1 - alpha_sig) * prev
        out[i] = prev
    return out`;

export default {
  id: 'macd-signal',
  name: 'MACD Signal',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `MACD Signal line — EMA of the MACD line (fast\_EMA(close) − slow\_EMA(close)) over \`signal\` bars. Seeded with the mean of the first \`signal\` valid MACD values.

**Parameters**
- \`fast\`: span of the fast EMA inside the MACD line. Typical value 12.
- \`slow\`: span of the slow EMA inside the MACD line. Must exceed \`fast\`. Typical value 26.
- \`signal\`: span of the EMA applied to the MACD line itself. Typical value 9.`,
};
