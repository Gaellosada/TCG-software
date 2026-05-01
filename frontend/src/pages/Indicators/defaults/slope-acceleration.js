// Slope Acceleration — difference between current-bar and prior-bar simple returns.
//   accel_t = (close_t/close_{t-1} - 1) - (close_{t-1}/close_{t-2} - 1)
// Under the scalar-per-bar contract only the accel scalar is returned.
const code = `def compute(series):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < 3:
        return out
    # Simple returns r_t = s_t / s_{t-1} - 1, valid from t=1.
    denom_curr = s[1:]     # s_{t-1} for t>=1 (i.e. shifted)
    # Use safe division: NaN where denominator is 0.
    r_all = np.full(n, np.nan, dtype=float)
    prev = s[:-1]
    safe_prev = np.where(prev != 0.0, prev, np.nan)
    r_all[1:] = s[1:] / safe_prev - 1.0
    # accel_t = r_t - r_{t-1}, valid from t=2.
    out[2:] = r_all[2:] - r_all[1:-1]
    return out`;

export default {
  id: 'slope-acceleration',
  name: 'Slope Acceleration',
  readonly: true,
  category: 'momentum',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** A cheap second-derivative proxy for price. It compares the current-bar simple return to the prior-bar simple return: positive values mean momentum is accelerating (current return > prior return), negative means decelerating, sign flips mean reversal. Traders use it to distinguish a rally that is losing steam from one that is still powering up.

**Formula.**
\`\`\`
r_t     = close_t / close_{t-1} - 1          (simple return)
accel_t = r_t - r_{t-1}
\`\`\`

**Parameters**
- None. The indicator is unparameterised.

**Edge cases**
- Output is \`NaN\` for the first two bars (need \`close_{t-2}\`).
- When \`close_{t-1} == 0\` or \`close_{t-2} == 0\` the corresponding return is \`NaN\`; the accel becomes \`NaN\` for the affected bars.
- \`NaN\` in the input propagates.

**Notes.** Users wanting the raw current return can use \`centred-slope\` (symmetric variant) or derive the simple return in one line in their own sandbox cell.`,
  ownPanel: true,
};
