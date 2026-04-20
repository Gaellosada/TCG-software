// Simple Moving Average — equally-weighted rolling mean over ``window`` bars.
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full_like(s, np.nan, dtype=float)
    if n < window:
        return out
    out[window-1:] = np.convolve(s, np.ones(window) / window, mode='valid')
    return out`;

export default {
  id: 'sma',
  name: 'SMA',
  readonly: true,
  category: 'trend',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** The Simple Moving Average smooths a price series by averaging the last \`window\` closes with equal weight. Traders use it as a baseline trend filter: price above the SMA suggests an uptrend, price below suggests a downtrend. Two SMAs of different lengths crossing one another is a classic trend-following signal.

**Formula.**
\`\`\`
SMA_t = (1 / window) * sum_{k = t - window + 1}^{t} close_k
\`\`\`

**Parameters**
- \`window\` (int, default 20): number of bars in the averaging window. Larger values smooth more aggressively but lag price more.

**Edge cases**
- Output is \`NaN\` for the first \`window - 1\` bars (warm-up).
- Any \`NaN\` in the close series poisons every output bar whose rolling window includes it — the \`NaN\` propagates for the full \`window\` bars following its position.
- No division-by-zero path — \`window\` is required to be \`>= 1\`.`,
  ownPanel: false,
};
