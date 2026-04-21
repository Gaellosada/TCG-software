// Historical Volatility — annualised rolling standard deviation of simple returns.
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    for i in range(window - 1, n):
        chunk = s[i - window + 1 : i + 1]
        rets = chunk[1:] / chunk[:-1] - 1.0
        out[i] = np.std(rets, ddof=1) * (252.0 ** 0.5)
    return out`;

export default {
  id: 'historical-vol',
  name: 'Historical Volatility',
  readonly: true,
  category: 'volatility',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** Historical Volatility measures how much an instrument's price has fluctuated over a recent window, expressed as an annualised percentage. It is the classic realised-volatility estimator: compute simple percentage returns over a rolling window, take their sample standard deviation, then scale to annual units by multiplying by \`sqrt(252)\`. Higher values mean the instrument has been moving more; lower values mean it has been calm. Traders use it to gauge regime (trending vs. range-bound), to compare implied volatility against realised, and to size positions.

**Formula.**
\`\`\`
ret_t     = close_t / close_{t-1} - 1          (simple percentage return)
hvol_t    = std(ret_{t-window+2} ... ret_t, ddof=1) * sqrt(252)
\`\`\`
where \`std(..., ddof=1)\` is the sample standard deviation (Bessel-corrected, denominator \`window - 2\` since there are \`window - 1\` returns in a window of \`window\` closes).

**Parameters**
- \`window\` (int, default 20): number of closing prices in the rolling window. The number of returns used is \`window - 1\`. Common choices: 20 (one trading month), 60 (one quarter), 252 (one year).

**Edge cases**
- Output is \`NaN\` for the first \`window - 1\` bars (warm-up period: fewer than \`window\` closes available).
- If all closes in a window are identical (zero returns), \`std\` is 0 and the output is 0 — not \`NaN\`.
- A single \`NaN\` in the close series will propagate through every window that contains it, producing \`NaN\` for \`window\` consecutive output bars.
- Uses simple percentage returns (\`close_t / close_{t-1} - 1\`), **not** log returns. The difference is negligible for small daily moves but diverges for large moves.`,
  ownPanel: true,
  chartMode: 'lines',
};
