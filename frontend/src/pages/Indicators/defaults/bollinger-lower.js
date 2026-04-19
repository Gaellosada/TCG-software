// Bollinger lower band — SMA(close, window) - num_std * rolling_stddev.
const code = `def compute(series, window: int = 20, num_std: float = 2.0):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    csum = np.concatenate((np.array([0.0]), np.cumsum(s)))
    csum2 = np.concatenate((np.array([0.0]), np.cumsum(s * s)))
    idx = np.arange(window - 1, n)
    win_sum = csum[idx + 1] - csum[idx - window + 1]
    win_sum2 = csum2[idx + 1] - csum2[idx - window + 1]
    mean = win_sum / window
    var = win_sum2 / window - mean * mean
    var = np.where(var < 0, 0.0, var)
    std = np.sqrt(var)
    out[window-1:] = mean - num_std * std
    return out`;

export default {
  id: 'bollinger-lower',
  name: 'Bollinger Lower',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
};
