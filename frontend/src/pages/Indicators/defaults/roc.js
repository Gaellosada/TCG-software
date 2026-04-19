// Rate of Change — percent return over ``window`` bars.
//   ROC[i] = (s[i] - s[i-window]) / s[i-window] * 100
const code = `def compute(series, window: int = 10):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n <= window:
        return out
    prev = s[:n-window]
    curr = s[window:]
    out[window:] = np.where(prev != 0, (curr - prev) / prev * 100.0, np.nan)
    return out`;

export default {
  id: 'roc',
  name: 'ROC',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Rate of Change — percentage price change over \`window\` bars: \`ROC[i] = (close[i] − close[i−window]) / close[i−window] × 100\`. NaN when the lagged close is zero.

**Parameters**
- \`window\`: lookback period in bars. Larger values measure longer-term momentum.`,
};
