// Kaufman Adaptive Moving Average.
//   ER = |s[i] - s[i-window]| / sum(|diff(s)|, window)
//   SC = (ER * (2/(fast+1) - 2/(slow+1)) + 2/(slow+1))^2
//   KAMA[i] = KAMA[i-1] + SC[i] * (s[i] - KAMA[i-1])
// Seeded with s[window-1]; outputs before that index are NaN.
const code = `def compute(series, window: int = 10, fast: int = 2, slow: int = 30):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n <= window:
        return out
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    abs_diff = np.abs(np.diff(s))
    # Prefix-sum of |diff| with a leading 0 so csum[i] - csum[i-window]
    # gives the rolling |diff| sum ending at s-index i.
    csum = np.concatenate((np.array([0.0]), np.cumsum(abs_diff)))
    out[window-1] = s[window-1]
    prev = s[window-1]
    for i in range(window, n):
        volatility = csum[i] - csum[i-window]
        change = s[i] - s[i-window]
        if volatility > 0:
            er = np.abs(change) / volatility
        else:
            er = 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = prev + sc * (s[i] - prev)
        out[i] = prev
    return out`;

export default {
  id: 'kama',
  name: 'KAMA',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Kaufman Adaptive Moving Average — self-adjusting MA over closing prices that tightens when price trends and widens when price chops. The smoothing constant \`SC\` is squared: \`SC = (ER·(fast_sc − slow_sc) + slow_sc)²\` where ER is the efficiency ratio over the lookback.

**Parameters**
- \`window\`: lookback for the efficiency ratio. Larger values make ER smoother and adaptation slower.
- \`fast\`: span of the fast EMA limit (used when ER ≈ 1, trending market). Typical value 2.
- \`slow\`: span of the slow EMA limit (used when ER ≈ 0, choppy market). Must exceed \`fast\`. Typical value 30.`,
};
