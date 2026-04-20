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
    # The 'n < slow + signal - 1' guard above ensures end <= n here.
    start = slow - 1
    end = start + signal
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
  category: 'momentum',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** The MACD Signal line is an EMA applied to the MACD Line itself. It acts as a smoothed trigger: when MACD crosses above its signal line traders view this as bullish, and crosses below as bearish. The signal line lags the MACD line by design so that noisy oscillations around zero don't each trigger a crossover.

**Formula.**
\`\`\`
MACD_t   = EMA(close, fast)_t - EMA(close, slow)_t
Signal_t = EMA(MACD, signal)_t
\`\`\`
The signal EMA is seeded with the mean of the first \`signal\` valid \`MACD\` values (i.e. starting at index \`slow - 1\`).

**Parameters**
- \`fast\` (int, default 12): span of the fast EMA inside the MACD line.
- \`slow\` (int, default 26): span of the slow EMA inside the MACD line. Must exceed \`fast\`.
- \`signal\` (int, default 9): span of the EMA applied to the MACD line.

**Edge cases**
- Output is \`NaN\` for the first \`slow + signal - 2\` bars (warm-up: MACD needs \`slow - 1\`, then signal needs \`signal\` valid MACDs to seed).
- If \`n < slow + signal - 1\` the output is all \`NaN\`.
- \`NaN\` in close before the MACD seed bar pollutes both EMAs and propagates.`,
  ownPanel: true,
};
