// Weighted Impetus — signed-magnitude rolling sum of per-bar deltas.
// Algebraically telescopes to close_t - close_{t-window}; emitted as the
// primary channel. A companion "stddev of deltas" channel using a
// non-standard sumAbs^2 correction existed upstream but is not surfaced
// here because compute() is scalar-return. See docs.
const code = `def compute(series, window: int = 14):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window + 1:
        return out
    # impetus_t = sum of (close_k - close_{k-1}) over last 'window' bars
    #           = close_t - close_{t-window}
    out[window:] = s[window:] - s[:-window]
    return out`;

export default {
  id: 'weighted-impetus',
  name: 'Weighted Impetus',
  readonly: true,
  category: 'momentum',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** Generalisation of \`impetus\`: instead of contributing \`+/-1\` per bar based only on the sign of the change, it contributes the **signed magnitude** \`close_t - close_{t-1}\`. The rolling sum over \`window\` bars telescopes to the net displacement \`close_t - close_{t-window}\`. High positive readings mean the price has travelled far upward over the window; negative readings mean the opposite. Sign-sensitive momentum that rewards big moves.

⚠️ **Note.** Despite the name "Weighted", this indicator does **NOT** use volume. The weighting refers to the magnitude of absolute price changes. Additionally, a companion volatility channel (not surfaced here) uses a non-standard correction term (based on the sum of absolute changes rather than the sum of signed changes), so numbers would not match a textbook standard deviation of returns.

**Formula.**
\`\`\`
delta_t    = close_t - close_{t-1}
impetus_t  = sum_{k = t - window + 1}^{t} delta_k
           = close_t - close_{t - window}    (telescoping)
\`\`\`

**Parameters**
- \`window\` (int, default 14): rolling-sum length. Larger values widen the displacement window and yield a slower-moving, smoother output; smaller values react faster but are noisier.

**Edge cases**
- Output is \`NaN\` for the first \`window\` bars (need \`close_{t - window}\`).
- \`NaN\` in the input propagates via the subtraction.
- The "Weighted" qualifier is **not** about volume weighting — see the note above.`,
  ownPanel: true,
};
