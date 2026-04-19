// Rolling population standard deviation (ddof=0) over ``window`` bars.
// Uses the cumsum trick for O(n): var = E[x^2] - (E[x])^2, then sqrt.
// Small negative variances from floating-point cancellation are clipped
// to zero before the sqrt.
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    csum = np.concatenate((np.array([0.0]), np.cumsum(s)))
    csum2 = np.concatenate((np.array([0.0]), np.cumsum(s * s)))
    # For output index i (i >= window-1), window covers s[i-window+1..i].
    # Sum over that window = csum[i+1] - csum[i-window+1].
    idx = np.arange(window - 1, n)
    win_sum = csum[idx + 1] - csum[idx - window + 1]
    win_sum2 = csum2[idx + 1] - csum2[idx - window + 1]
    mean = win_sum / window
    var = win_sum2 / window - mean * mean
    var = np.where(var < 0, 0.0, var)
    out[window-1:] = np.sqrt(var)
    return out`;

export default {
  id: 'rolling-stddev',
  name: 'Rolling StdDev',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
};
