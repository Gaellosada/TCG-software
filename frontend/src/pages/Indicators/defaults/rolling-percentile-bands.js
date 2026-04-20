// Rolling Percentile Bands — one selected percentile over a rolling window.
// Sandbox contract is scalar-per-bar so this exposes a single ``percentile``
// param (0..100). Users wanting multiple bands instantiate the indicator
// multiple times.
const code = `def compute(series, window: int = 252, percentile: float = 95.0):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window or percentile < 0.0 or percentile > 100.0:
        return out
    # Nearest-rank: convert percentile (0..100) to a 0-indexed rank into the
    # sorted window of length 'window'. Clip to the valid range so 100.0
    # maps to the top order statistic rather than overflowing the index.
    rank = int(percentile * window / 100.0)
    if rank >= window:
        rank = window - 1
    for t in range(window - 1, n):
        w = s[t - window + 1 : t + 1]
        # np.partition for O(n) selection of the rank-th order statistic.
        w_clean = w[~np.isnan(w)]
        if w_clean.shape[0] < window:
            # NaNs in window — emit NaN for that bar (the sorted window
            # is not well-defined in that case).
            continue
        out[t] = np.partition(w_clean, rank)[rank]
    return out`;

export default {
  id: 'rolling-percentile-bands',
  name: 'Rolling Percentile Bands',
  readonly: true,
  category: 'statistical',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** For every bar, maintains a sorted rolling window of the last \`window\` closes and emits the value at the specified **nearest-rank** percentile. Used for non-parametric entry / exit bands: "enter long when close exceeds the 95th percentile of the last 252 closes, exit when it drops below the 80th". Robust to outliers and regime-adaptive — the threshold tracks the empirical distribution.

> ⚠️ **\`percentile\` is on a 0..100 scale, NOT a 0..1 fraction.** The default \`percentile = 95.0\` means the 95th percentile. Passing \`0.95\` would select a very low order statistic (rank 0 for any reasonable window) and produce a near-minimum of the window rather than an upper band. Values outside \`[0, 100]\` produce all-NaN output.

**Formula.**
\`\`\`
W_t    = sort({close_{t - window + 1}, ..., close_t})       (ascending)
rank   = floor(percentile * window / 100)                    (clipped to window - 1)
out_t  = W_t[rank]                                          (nearest-rank, no interpolation)
\`\`\`

**Parameters**
- \`window\` (int, default 252): rolling window length.
- \`percentile\` (float, default 95.0): percentile to emit, on a 0..100 scale. Internally translated to a 0-indexed rank \`floor(percentile * window / 100)\` (clipped so \`percentile = 100\` maps to the largest observation in the window).

**Edge cases**
- Output is \`NaN\` for the first \`window - 1\` bars (warm-up).
- If any \`NaN\` is present in the window the output for that bar is \`NaN\` (the full sorted window is not well-defined).
- If \`percentile\` is outside \`[0, 100]\` the output is all \`NaN\`.
- Uses **nearest-rank**, no interpolation — output values are always actual observed prices from the window.`,
  ownPanel: false,
};
