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
  category: 'momentum',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** The Relative Strength Index is Wilder's bounded momentum oscillator. It compares the average magnitude of up-moves to the average magnitude of down-moves over the last \`window\` bars and maps the ratio onto \`[0, 100]\`. Readings above 70 are traditionally "overbought", below 30 "oversold". In practice traders use it to spot momentum exhaustion, divergences with price, and breakouts of its midline.

**Formula.**
\`\`\`
diff_t     = close_t - close_{t-1}
gain_t     = max(diff_t, 0)
loss_t     = max(-diff_t, 0)
avg_gain_t = ((window-1) * avg_gain_{t-1} + gain_t) / window      (Wilder smoothing)
avg_loss_t = ((window-1) * avg_loss_{t-1} + loss_t) / window
RS_t       = avg_gain_t / avg_loss_t
RSI_t      = 100 - 100 / (1 + RS_t)
\`\`\`
The first \`avg_gain\` / \`avg_loss\` are seeded with the simple mean of the first \`window\` gains / losses.

**Parameters**
- \`window\` (int, default 14): lookback for Wilder's smoothing of the gain / loss averages. Smaller values react faster but whipsaw more.

**Edge cases**
- Output is \`NaN\` for the first \`window\` bars (warm-up — the first RSI is emitted at index \`window\`).
- When the rolling \`avg_loss\` is exactly zero the ratio \`RS\` is treated as \`+inf\` and \`RSI\` pins to 100.
- When both averages are zero (flat series) \`RS\` stays at \`+inf\` and \`RSI\` also pins to 100; the indicator cannot distinguish flat from strictly rising in that degenerate case.
- \`NaN\` in the input propagates through \`np.diff\` and contaminates subsequent smoothed values until the window is clean again.`,
  ownPanel: true,
};
