// MACD histogram — MACD line minus MACD signal line.
// Computed fully inline so every entry is self-contained.
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
    sig = np.full(n, np.nan, dtype=float)
    # The 'n < slow + signal - 1' guard above ensures end <= n here.
    start = slow - 1
    end = start + signal
    seed_sig = np.mean(macd[start:end])
    sig[end - 1] = seed_sig
    prev = seed_sig
    for i in range(end, n):
        prev = alpha_sig * macd[i] + (1 - alpha_sig) * prev
        sig[i] = prev
    out = macd - sig
    return out`;

export default {
  id: 'macd-histogram',
  name: 'MACD Histogram',
  readonly: true,
  category: 'momentum',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** The MACD Histogram is the gap between the MACD Line and the MACD Signal — positive when the MACD is above its signal (bullish momentum), negative when below. Histogram bars shrinking towards zero warn that the current momentum phase is fading; a change of sign is a crossover event. It is often the most actionable view of the MACD family.

**Formula.**
\`\`\`
MACD_t      = EMA(close, fast)_t - EMA(close, slow)_t
Signal_t    = EMA(MACD, signal)_t
Histogram_t = MACD_t - Signal_t
\`\`\`

**Parameters**
- \`fast\` (int, default 12): span of the fast EMA.
- \`slow\` (int, default 26): span of the slow EMA. Must exceed \`fast\`.
- \`signal\` (int, default 9): span of the EMA applied to the MACD line.

**Edge cases**
- Output is \`NaN\` for the first \`slow + signal - 2\` bars (same warm-up as MACD Signal).
- If \`n < slow + signal - 1\` the output is all \`NaN\`.
- Inherits \`NaN\` propagation from close → EMAs → MACD → signal.`,
  ownPanel: true,
};
