// Log return over ``window`` bars — log(s[i] / s[i-window]).
// Undefined (NaN) when the lagged value is <= 0.
const code = `def compute(series, window: int = 1):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n <= window:
        return out
    prev = s[:n-window]
    curr = s[window:]
    ratio = np.where(prev > 0, curr / prev, np.nan)
    out[window:] = np.log(np.where(ratio > 0, ratio, np.nan))
    return out`;

export default {
  id: 'log-return',
  name: 'Log Return',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
};
