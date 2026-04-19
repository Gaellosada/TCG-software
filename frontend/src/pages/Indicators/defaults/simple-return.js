// Simple return over ``window`` bars — (s[i] - s[i-window]) / s[i-window].
// Undefined (NaN) when the lagged value is zero.
const code = `def compute(series, window: int = 1):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n <= window:
        return out
    prev = s[:n-window]
    curr = s[window:]
    out[window:] = np.where(prev != 0, (curr - prev) / prev, np.nan)
    return out`;

export default {
  id: 'simple-return',
  name: 'Simple Return',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Simple (arithmetic) return of closing prices over \`window\` bars: \`(close[i] − close[i−window]) / close[i−window]\`. NaN when the lagged close is zero. Default \`window = 1\` gives bar-by-bar returns.

**Parameters**
- \`window\`: number of bars in the return horizon. Increase for multi-bar period returns.`,
  ownPanel: true,
};
