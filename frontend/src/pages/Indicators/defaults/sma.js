// Simple Moving Average — equally-weighted rolling mean over ``window`` bars.
const code = `def compute(series, window: int = 20):
    s = series['close']
    out = np.full_like(s, np.nan, dtype=float)
    out[window-1:] = np.convolve(s, np.ones(window) / window, mode='valid')
    return out`;

export default {
  id: 'sma',
  name: 'SMA',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
};
