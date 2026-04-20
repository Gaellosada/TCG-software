// Percentile-Filtered Return — rolling percentile of the close-vs-SMA deviation.
// Input to the percentile machinery is r_t = (close_t - sma_t) / sma_t,
// where sma_t is an SMA of close over ``filter_window``. Primary emitted
// channel (under the scalar-per-bar contract) is the selected rank of r_t.
const code = `def compute(series, window: int = 252, filter_window: int = 50, percentile: float = 95.0):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < max(window, filter_window) or percentile < 0.0 or percentile > 100.0:
        return out
    # SMA of close over filter_window.
    sma = np.full(n, np.nan, dtype=float)
    sma[filter_window - 1:] = np.convolve(s, np.ones(filter_window) / filter_window, mode='valid')
    # r_t = (close_t - sma_t) / sma_t ; NaN where sma is NaN or zero.
    safe_sma = np.where((~np.isnan(sma)) & (sma != 0.0), sma, np.nan)
    r = (s - safe_sma) / safe_sma
    # Convert percentile (0..100) to a 0-indexed rank over the window.
    rank = int(percentile * window / 100.0)
    if rank >= window:
        rank = window - 1
    # Rolling percentile of r over 'window' deviations.
    first_valid = filter_window - 1 + window - 1
    for t in range(first_valid, n):
        w = r[t - window + 1 : t + 1]
        w_clean = w[~np.isnan(w)]
        if w_clean.shape[0] < window:
            continue
        out[t] = np.partition(w_clean, rank)[rank]
    return out`;

export default {
  id: 'percentile-filtered-return',
  name: 'Percentile-Filtered Return',
  readonly: true,
  category: 'statistical',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** First computes a smooth reference (an SMA of close over \`filter_window\`), then measures how far current close sits above or below that reference as a percentage: \`r_t = (close_t - sma_t) / sma_t\`. Then takes a nearest-rank percentile of \`r_t\` over the last \`window\` bars. A classic breakout trigger: with the default \`percentile = 95\` the output marks the upper 5% of the historical close-vs-SMA deviation distribution — flag when \`r_t\` reaches the top 5% of its recent range. For the symmetric lower-tail (mean-reversion) use-case, instantiate a second copy with \`percentile = 5\`.

> ⚠️ **\`percentile\` is on a 0..100 scale, NOT a 0..1 fraction.** The default \`percentile = 95.0\` means the 95th percentile of the deviation stream. Passing \`0.95\` would select rank 0 (a near-minimum deviation) and defeat the intended upper-tail trigger. Values outside \`[0, 100]\` produce all-NaN output.

**Formula.**
\`\`\`
sma_t = (1 / filter_window) * sum_{k = t - filter_window + 1}^{t} close_k
r_t   = (close_t - sma_t) / sma_t
W_t   = sort({r_{t - window + 1}, ..., r_t})          (ascending)
rank  = floor(percentile * window / 100)               (clipped to window - 1)
out_t = W_t[rank]                                      (nearest-rank)
\`\`\`

**Parameters**
- \`window\` (int, default 252): rolling window for the percentile over the deviation stream.
- \`filter_window\` (int, default 50): length of the SMA used as the smooth reference.
- \`percentile\` (float, default 95.0): percentile to emit, on a 0..100 scale. Translated internally to a 0-indexed rank into the sorted deviation window.

**Edge cases**
- Output is \`NaN\` for the first \`filter_window + window - 2\` bars (warm-up for SMA, then for the percentile window).
- When \`sma_t == 0\` the deviation is undefined; that bar contributes \`NaN\` to the percentile window and downstream output becomes \`NaN\` until the window is clean.
- \`NaN\` in the input propagates through the SMA.
- \`percentile\` outside \`[0, 100]\` → output all \`NaN\`.
- Uses **nearest-rank** — output values are always actual observed deviations, not interpolated.

**Notes on composition.** The smooth reference is fused with an SMA of close by default. Users wanting a different reference (EMA, Kalman) can pre-smooth in an upstream cell and feed that series in place of close.`,
  ownPanel: true,
};
