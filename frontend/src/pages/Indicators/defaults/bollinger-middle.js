// Bollinger middle band — the SMA of close over ``window`` bars.
// Kept as a separate entry so the UI can render middle/upper/lower as
// three distinct overlays; numerically identical to the ``sma`` entry.
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full_like(s, np.nan, dtype=float)
    if n < window:
        return out
    out[window-1:] = np.convolve(s, np.ones(window) / window, mode='valid')
    return out`;

export default {
  id: 'bollinger-middle',
  name: 'Bollinger Middle',
  readonly: true,
  category: 'volatility',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** The Bollinger Middle band is the simple moving average of close that sits in the centre of the Bollinger channel. It is numerically identical to the SMA indicator but is surfaced separately so upper / middle / lower render as three distinct overlays on the same chart. Practitioners read crossings of price through the middle band as a trend filter.

**Formula.**
\`\`\`
middle_t = (1 / window) * sum_{k = t - window + 1}^{t} close_k
\`\`\`

**Parameters**
- \`window\` (int, default 20): rolling window for the average. Larger values smooth more and lag price more.

**Edge cases**
- Output is \`NaN\` for the first \`window - 1\` bars (warm-up).
- \`NaN\` anywhere in the last \`window\` closes propagates to the output for that bar.
- Must use the same \`window\` as \`bollinger-upper\` / \`bollinger-lower\` for the three overlays to be consistent.`,
  ownPanel: false,
};
