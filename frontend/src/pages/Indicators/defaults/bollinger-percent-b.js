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
  category: 'volatility',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** Bollinger %B normalises the location of close within the Bollinger channel onto \`[0, 1]\`: \`%B = 0\` means price sits exactly on the lower band, \`%B = 1\` means it sits exactly on the upper band, \`%B = 0.5\` means it sits at the middle band. Values outside \`[0, 1]\` indicate price has broken through the respective band — a common momentum-extreme signal.

**Formula.**
\`\`\`
mean_t   = (1 / window) * sum_{k = t - window + 1}^{t} close_k
var_t    = (1 / window) * sum_{k = t - window + 1}^{t} close_k^2 - mean_t^2
upper_t  = mean_t + num_std * sqrt(var_t)
lower_t  = mean_t - num_std * sqrt(var_t)
%B_t     = (close_t - lower_t) / (upper_t - lower_t)
\`\`\`

**Parameters**
- \`window\` (int, default 20): rolling window for the SMA and standard deviation.
- \`num_std\` (float, default 2.0): standard-deviation multiplier defining band width.

**Edge cases**
- Output is \`NaN\` for the first \`window - 1\` bars (warm-up).
- When \`upper_t - lower_t == 0\` (flat series inside the window) the division is undefined; output is \`NaN\` for that bar.
- \`%B\` can legitimately take values outside \`[0, 1]\` when price breaks a band; this is a feature, not a clipping bug.
- \`NaN\` anywhere in the window propagates through \`np.cumsum\` for the rest of the series.`,
  ownPanel: true,
};
