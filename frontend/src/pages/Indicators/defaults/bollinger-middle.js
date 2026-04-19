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
};
