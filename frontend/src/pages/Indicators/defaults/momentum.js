// Momentum — absolute change over ``window`` bars.
//   M[i] = s[i] - s[i-window]
const code = `def compute(series, window: int = 10):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n <= window:
        return out
    out[window:] = s[window:] - s[:n-window]
    return out`;

export default {
  id: 'momentum',
  name: 'Momentum',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Momentum — absolute price change over \`window\` bars: \`M[i] = close[i] − close[i−window]\`. Unlike ROC it is not normalised, so magnitude depends on price level.

**Parameters**
- \`window\`: lookback period in bars. Larger values capture longer-term directional moves.`,
  ownPanel: true,
};
