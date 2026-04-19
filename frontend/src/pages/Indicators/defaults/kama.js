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
    # Rolling sum of abs_diff over ``window`` elements; cumsum trick gives
    # length n-1-(window-1) = n-window values starting at index window-1 of s.
    csum = np.concatenate((np.array([0.0]), np.cumsum(abs_diff)))
    # volatility[k] corresponds to s-index (window + k - 1) for k in [0, n-window]
    # Here we align directly to s-indices i in [window, n-1].
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
};
