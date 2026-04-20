// Centred Slope — midpoint-normalised per-bar change.
//   slope_t = (close_t - close_{t-1}) / ((close_t + close_{t-1}) / 2)
// Symmetric in (close_t, close_{t-1}); differs from plain simple return.
const code = `def compute(series, window: int = 1):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n <= window:
        return out
    num = s[window:] - s[:-window]
    denom = (s[window:] + s[:-window]) / 2.0
    # Avoid div-by-zero without suppressing the legitimate zero numerator case.
    safe = np.where(denom != 0.0, denom, np.nan)
    out[window:] = num / safe
    return out`;

export default {
  id: 'centred-slope',
  name: 'Centred Slope',
  readonly: true,
  category: 'momentum',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** The Centred Slope is a symmetric alternative to the simple return: instead of dividing the change by the prior value, it divides by the **midpoint** of the two values. Swapping \`close_t\` and \`close_{t-window}\` flips the sign exactly, which plain simple return does not do. For small changes the centred slope is approximately equal to the log-return and is the second-order Taylor approximation thereof.

**Formula.**
\`\`\`
slope_t = (close_t - close_{t-window}) / ((close_t + close_{t-window}) / 2)
        = 2 * (close_t - close_{t-window}) / (close_t + close_{t-window})
\`\`\`

**Parameters**
- \`window\` (int, default 1): lag. \`window = 1\` is the canonical consecutive-bars form. Larger values widen the comparison.

**Edge cases**
- Output is \`NaN\` for the first \`window\` bars (need \`close_{t - window}\`).
- When \`close_t + close_{t - window} == 0\` (zero-mean or sign-flipping series) the denominator is zero and the output is \`NaN\` for that bar.
- \`NaN\` in the input propagates via the subtraction.`,
  ownPanel: true,
};
