// Relative Strength Index — Wilder's smoothing.
// Gains/losses are the positive/absolute-negative parts of daily diffs.
// The first RS uses the simple mean of the first ``window`` diffs; subsequent
// values use Wilder's recursion: avg[i] = ((window-1)*avg[i-1] + x[i]) / window.
const code = `def compute(series, window: int = 14):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n <= window:
        return out
    diff = np.diff(s)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_gain = np.mean(gains[:window])
    avg_loss = np.mean(losses[:window])
    if avg_loss == 0:
        rs = np.inf
    else:
        rs = avg_gain / avg_loss
    out[window] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(window + 1, n):
        g = gains[i-1]
        l = losses[i-1]
        avg_gain = ((window - 1) * avg_gain + g) / window
        avg_loss = ((window - 1) * avg_loss + l) / window
        if avg_loss == 0:
            rs = np.inf
        else:
            rs = avg_gain / avg_loss
        out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out`;

export default {
  id: 'rsi',
  name: 'RSI',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Relative Strength Index — bounded oscillator in [0, 100] over closing prices: \`RSI = 100 − 100 / (1 + avg_gain / avg_loss)\`. Uses Wilder's smoothing; first RS is seeded with the simple mean of the first \`window\` up/down moves.

**Parameters**
- \`window\`: lookback for average gain/loss. Typical value 14. Smaller window → more reactive; larger → smoother.

**Notes**
- Conventional overbought/oversold thresholds are 70 and 30.`,
  ownPanel: true,
};
