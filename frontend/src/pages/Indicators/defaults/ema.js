// Exponential Moving Average — recursive filter with alpha = 2 / (window + 1).
// Seeded with the SMA of the first ``window`` values so the recursion has a
// stable anchor; outputs before index ``window-1`` are NaN.
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    alpha = 2.0 / (window + 1)
    seed = np.mean(s[:window])
    out[window-1] = seed
    prev = seed
    for i in range(window, n):
        prev = alpha * s[i] + (1 - alpha) * prev
        out[i] = prev
    return out`;

export default {
  id: 'ema',
  name: 'EMA',
  readonly: true,
  category: 'trend',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** The Exponential Moving Average smooths a price series by recursively blending today's close with yesterday's EMA. Weights decay geometrically with age, so recent prices dominate and older prices fade quickly. Compared to the SMA, the EMA reacts faster to price changes for the same nominal window.

**Formula.**
\`\`\`
alpha   = 2 / (window + 1)
EMA_t   = alpha * close_t + (1 - alpha) * EMA_{t-1}
EMA_{window-1} = SMA(close[0..window-1])          (seed)
\`\`\`

**Parameters**
- \`window\` (int, default 20): nominal span of the EMA. Controls \`alpha\`. Smaller values react faster but are noisier; larger values are smoother but lag more.

**Edge cases**
- Output is \`NaN\` for the first \`window - 1\` bars (warm-up while seeding).
- If the input has fewer than \`window\` bars the output is all \`NaN\`.
- A \`NaN\` close before the seed bar will pollute the seed via \`np.mean\`; clean the input upstream.`,
  ownPanel: false,
};
