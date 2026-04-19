// Rolling z-score — (s - rolling_mean) / rolling_stddev, population std.
// Undefined (NaN) when the rolling std is zero.
const code = `def compute(series, window: int = 20):
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
    slice_s = s[window-1:]
    out[window-1:] = np.where(std > 0, (slice_s - mean) / std, np.nan)
    return out`;

export default {
  id: 'rolling-zscore',
  name: 'Rolling Z-Score',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Rolling Z-Score — number of standard deviations the current close is from its rolling mean: \`(close − mean) / σ\`, using population standard deviation. NaN when the rolling std is zero.

**Parameters**
- \`window\`: rolling window for mean and standard deviation. Smaller window → more sensitive to recent price levels.`,
};
