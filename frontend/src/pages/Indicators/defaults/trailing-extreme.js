// Trailing Extreme — rolling max (or min) of close over ``window`` bars.
// Single ``mode`` parameter selects direction. The JS sandbox contract only
// admits int/float/bool typed literal defaults (no strings), so the mode is
// exposed as a boolean ``use_min`` flag — False => rolling max, True => min.
const code = `def compute(series, window: int = 20, use_min: bool = False):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window:
        return out
    # Naive O(n*window) rolling reduction; fine for typical UI use.
    # Uses nanmax/nanmin so a NaN in the window does not destroy the result.
    for t in range(window - 1, n):
        w = s[t - window + 1 : t + 1]
        if use_min:
            out[t] = np.nanmin(w)
        else:
            out[t] = np.nanmax(w)
    return out`;

export default {
  id: 'trailing-extreme',
  name: 'Trailing Extreme',
  readonly: true,
  category: 'pattern',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** Rolling maximum or minimum of close over the last \`window\` bars. Used as a chandelier-style trailing stop (max minus \`k * ATR\`), as a breakout level (Donchian-style), or to define the envelope of recent price action. With \`use_min = false\` you track the rolling high; with \`use_min = true\` you track the rolling low.

**Formula.**
\`\`\`
out_t = max_{k = t - window + 1}^{t}  close_k       (use_min = false)
out_t = min_{k = t - window + 1}^{t}  close_k       (use_min = true)
\`\`\`

**Parameters**
- \`window\` (int, default 20): lookback depth.
- \`use_min\` (bool, default False): direction flag. \`False\` → rolling maximum; \`True\` → rolling minimum. A \`mode: 'max' | 'min'\` string would read more naturally, but the sandbox parameter contract only accepts \`int\` / \`float\` / \`bool\` literal defaults, so the direction is expressed as a boolean flag instead.

**Edge cases**
- Output is \`NaN\` for the first \`window - 1\` bars (warm-up).
- Uses \`np.nanmax\` / \`np.nanmin\` so a single \`NaN\` inside the window is ignored rather than poisoning the output.
- Ties are handled transparently; both max and min are well-defined in the presence of repeated values.`,
  ownPanel: false,
};
