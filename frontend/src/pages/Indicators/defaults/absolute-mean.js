// Rolling Absolute Mean — applies abs consistently in both the initial
// window and incremental updates (a known inconsistency in some reference
// implementations is deliberately fixed here — see doc field).
const code = `def compute(series, window: int = 20):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    abs_s = np.abs(s)
    csum = np.concatenate((np.array([0.0]), np.cumsum(abs_s)))
    idx = np.arange(window - 1, n)
    win_sum = csum[idx + 1] - csum[idx - window + 1]
    out[window - 1:] = win_sum / window
    return out`;

export default {
  id: 'absolute-mean',
  name: 'Rolling Absolute Mean',
  readonly: true,
  category: 'statistical',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** Rolling mean of the **absolute value** of the input over \`window\` bars. Used when the sign of the input is noise but magnitude carries information — e.g. averaging \`|slope|\`, \`|return|\`, or any stream where positive and negative excursions are equally informative. Differs from SMA by the \`abs\` wrap: an alternating \`+5, -5\` series would SMA to zero but absolute-mean to 5.

⚠️ **Note.** This indicator takes the absolute value of every sample **before averaging, consistently across both the initial window and incremental updates**. Some implementations encountered elsewhere (including the one this design was derived from) apply \`abs\` only at initialization, producing results that differ depending on whether the indicator was warmed up from cold or updated bar-by-bar — this port fixes that inconsistency.

**Formula.**
\`\`\`
out_t = (1 / window) * sum_{k = t - window + 1}^{t} |close_k|
\`\`\`

**Parameters**
- \`window\` (int, default 20): averaging window. Larger values smooth more and lag more.

**Edge cases**
- Output is \`NaN\` for the first \`window - 1\` bars (warm-up).
- \`NaN\` in the input poisons \`np.cumsum\` and propagates forward.
- No division-by-zero path — \`window >= 1\`.`,
  ownPanel: false,
};
