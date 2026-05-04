// Impetus — sign-rolling-sum of per-bar step direction over ``window`` bars.
// Ties ( close_t == close_{t-1} ) count as +1.
const code = `def compute(series, window: int = 14):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window + 1:
        return out
    diff = np.diff(s)
    sign = np.where(diff >= 0, 1, -1).astype(float)
    # sign has length n-1 (indices 1..n-1 of the series).
    csum = np.concatenate((np.array([0.0]), np.cumsum(sign)))
    idx = np.arange(window - 1, n - 1)
    win_sum = csum[idx + 1] - csum[idx - window + 1]
    # First valid output is at series index 'window' (needs window signs).
    out[window:] = win_sum
    return out`;

export default {
  id: 'impetus',
  name: 'Impetus',
  readonly: true,
  category: 'momentum',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** Impetus counts up-bars minus down-bars over a rolling window: each bar where close rose contributes \`+1\`, each bar where close fell contributes \`-1\`. The rolling sum sits in \`[-window, +window]\`. Near \`+window\` means a persistent uptrend, near \`-window\` a persistent downtrend, near zero a choppy regime. A robustified momentum reading that ignores magnitude entirely — useful when outliers would distort a signed-return sum.

**Formula.**
\`\`\`
sign_t     = +1  if close_t >= close_{t-1}
             -1  otherwise
impetus_t  = sum_{k = t - window + 1}^{t} sign_k      in [-window, +window]
\`\`\`
Ties (\`close_t == close_{t-1}\`) count as \`+1\` by convention.

**Parameters**
- \`window\` (int, default 14): rolling-sum length. Larger values smooth more and widen the output range.

**Edge cases**
- Output is \`NaN\` for the first \`window\` bars (bar 0 has no prior close; then \`window\` signs must accumulate).
- \`NaN\` in the input poisons \`np.diff\` and propagates.
- Only the sign of each step matters; magnitudes are discarded.`,
  ownPanel: true,
};
