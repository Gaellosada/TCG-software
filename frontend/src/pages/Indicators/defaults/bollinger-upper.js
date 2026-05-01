// Bollinger upper band — SMA(close, window) + num_std * rolling_stddev.
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
    out[window-1:] = mean + num_std * std
    return out`;

export default {
  id: 'bollinger-upper',
  name: 'Bollinger Upper',
  readonly: true,
  category: 'volatility',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** The Bollinger Upper band is an SMA of close plus a multiple of the rolling (population) standard deviation of close. It defines a statistical ceiling that adapts to recent volatility: the band widens in volatile regimes and tightens in calm ones. Price touching or piercing the upper band is often read as an over-extension of the current move.

**Formula.**
\`\`\`
mean_t   = (1 / window) * sum_{k = t - window + 1}^{t} close_k
var_t    = (1 / window) * sum_{k = t - window + 1}^{t} close_k^2 - mean_t^2
upper_t  = mean_t + num_std * sqrt(var_t)
\`\`\`
Variance is clipped at zero to protect against floating-point negatives from \`E[x^2] - E[x]^2\` cancellation.

**Parameters**
- \`window\` (int, default 20): rolling window for mean and standard deviation. Larger values produce wider, slower-moving bands.
- \`num_std\` (float, default 2.0): multiplier on the rolling standard deviation. Larger values widen the channel.

**Edge cases**
- Output is \`NaN\` for the first \`window - 1\` bars (warm-up).
- Uses the population variance (denominator \`window\`, not \`window - 1\`); matches the canonical Bollinger definition.
- \`NaN\` anywhere in the window poisons \`np.cumsum\` and propagates forward for the rest of the series.`,
  ownPanel: false,
};
