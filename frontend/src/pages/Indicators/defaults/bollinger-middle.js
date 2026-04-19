// Bollinger middle band — the SMA of close over ``window`` bars.
// Kept as a separate entry so the UI can render middle/upper/lower as
// three distinct overlays; numerically identical to the ``sma`` entry.
const code = `def compute(series, window: int = 20):
    s = series['close']
    out = np.full_like(s, np.nan, dtype=float)
    out[window-1:] = np.convolve(s, np.ones(window) / window, mode='valid')
    return out`;

export default {
  id: 'bollinger-middle',
  name: 'Bollinger Middle',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Bollinger Middle Band — simple moving average of closing prices over \`window\` bars; numerically identical to the SMA entry but kept separate so upper/middle/lower render as three distinct overlays.

**Parameters**
- \`window\`: rolling window for the average. Larger values smooth more and lag price more.`,
};
