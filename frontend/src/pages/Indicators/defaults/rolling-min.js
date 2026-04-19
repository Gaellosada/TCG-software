// Rolling minimum of close over ``window`` bars.
// Straight Python loop — cheap (n iterations * O(window)) and well inside
// the 5-second sandbox budget for typical price-series lengths.
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    for i in range(window - 1, n):
        out[i] = np.min(s[i - window + 1:i + 1])
    return out`;

export default {
  id: 'rolling-min',
  name: 'Rolling Min',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Rolling minimum of closing prices over \`window\` bars. Useful as a support level proxy or in channel breakout conditions.

**Parameters**
- \`window\`: lookback period. Larger values track longer-term lows.`,
};
