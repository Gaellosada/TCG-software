// Slope Statistics — rolling sample stddev of per-bar simple returns.
// The scalar-per-bar sandbox contract admits only one channel; the stddev
// of returns is surfaced as the most useful summary. Mean-of-returns and
// raw return are trivially derived by the user if needed.
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window + 1 or window < 2:
        return out
    prev = s[:-1]
    safe_prev = np.where(prev != 0.0, prev, np.nan)
    r = s[1:] / safe_prev - 1.0   # length n-1
    # Rolling sample stddev of r over 'window' returns, valid from t=window.
    csum = np.concatenate((np.array([0.0]), np.cumsum(r)))
    csum2 = np.concatenate((np.array([0.0]), np.cumsum(r * r)))
    m = r.shape[0]
    idx = np.arange(window - 1, m)
    win_sum = csum[idx + 1] - csum[idx - window + 1]
    win_sum2 = csum2[idx + 1] - csum2[idx - window + 1]
    # Sample variance: ((sum_sq * n) - sum^2) / (n * (n - 1))
    var = (win_sum2 * window - win_sum * win_sum) / (window * (window - 1))
    var = np.where(var < 0, 0.0, var)
    std = np.sqrt(var)
    # First valid series-index is 'window' (needs window returns).
    out[window:] = std
    return out`;

export default {
  id: 'slope-statistics',
  name: 'Slope Statistics',
  readonly: true,
  category: 'statistical',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** Rolling **sample standard deviation** of per-bar simple returns — a real-time estimator of return volatility. Widely used as the volatility input in Sharpe-style ratios, z-scoring returns, regime detection, and position sizing (e.g. targeting constant risk). The rolling mean of returns (drift) is not surfaced here, since \`compute\` returns a single series; it is one line of sandbox code for users who need it.

**Formula.**
\`\`\`
r_t      = close_t / close_{t-1} - 1
var_t    = (sum_sq_t * n - sum_t^2) / (n * (n - 1))     (sample / Bessel-corrected)
stddev_t = sqrt(var_t)
\`\`\`
where \`n = window\`, \`sum_t = sum_{k} r_k\`, \`sum_sq_t = sum_{k} r_k^2\` over the last \`window\` returns.

**Parameters**
- \`window\` (int, default 20): rolling window for the stddev. Must be \`>= 2\` (Bessel correction divides by \`n - 1\`).

**Edge cases**
- Output is \`NaN\` for the first \`window\` bars (bar 0 has no return, then \`window\` returns must accumulate).
- If \`window < 2\` the output is all \`NaN\` (undefined sample stddev).
- Variance is clipped at zero to guard against floating-point negatives.
- When any \`close_{t-1} == 0\` the return is \`NaN\` and propagates through the window.`,
  ownPanel: true,
};
