// Bollinger %B — normalised position of close within the bands.
//   upper = SMA + num_std * std
//   lower = SMA - num_std * std
//   %B    = (s - lower) / (upper - lower) = (s - SMA + k*std) / (2*k*std)
// Undefined when the band width is zero (flat series) — NaN in that case.
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
    upper = mean + num_std * std
    lower = mean - num_std * std
    width = upper - lower
    slice_s = s[window-1:]
    pb = np.where(width > 0, (slice_s - lower) / width, np.nan)
    out[window-1:] = pb
    return out`;

export default {
  id: 'bollinger-percent-b',
  name: 'Bollinger %B',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Bollinger %B — normalised position of closing price within the Bollinger bands: \`%B = (close − lower) / (upper − lower)\`. Value 0 = at lower band, 1 = at upper band. NaN when band width is zero (flat series).

**Parameters**
- \`window\`: rolling window for the SMA and standard deviation.
- \`num_std\`: standard-deviation multiplier defining band width. Typical value 2.0.`,
};
