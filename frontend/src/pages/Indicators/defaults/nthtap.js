// NthTap — rolling count of level "taps".
// A "tap" is ANY crossing of the smoothed close through `level` (up OR down).
// The output is the rolling count of taps in the trailing `window` bars — a
// measure of how often price is oscillating around the level (chop / contact
// frequency). There is intentionally NO `n` parameter: the count threshold
// lives in the user's Compare(ge N) in a signal, so the same indicator drives
// "tapped at least N times" for any N without re-instantiating it.
const code = `def compute(series, level: float = 100.0, window: int = 20, ma_window: int = 20):
    assert window >= 1, 'window must be >= 1'
    assert ma_window >= 1, 'ma_window must be >= 1'
    s = series['close']
    n = s.shape[0]
    ma = np.full(n, np.nan, dtype=float)
    if n >= ma_window:
        ma[ma_window - 1:] = np.convolve(s, np.ones(ma_window) / ma_window, mode='valid')
    cu = ta.crossed_up(ma, level)
    cd = ta.crossed_down(ma, level)
    # A tap = any crossing of the level. up/down crosses are mutually
    # exclusive at a bar, so max() == logical-or; NaN propagates.
    tap = np.where(np.isnan(cu), np.nan, np.maximum(cu, cd))
    out = ta.count_in_window(tap, window)
    return out`;

export default {
  id: 'nthtap',
  name: 'NthTap',
  readonly: true,
  category: 'pattern',
  compatibleAssetTypes: ['index', 'equity'],
  chartShape: 'time-series',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** NthTap counts how many times the smoothed price has *tapped* a reference level recently — where a "tap" is any crossing of \`level\`, in either direction. A high count means price is repeatedly poking through the level (choppy, indecisive contact); a low count means the level is rarely touched. It answers "how many times has price crossed \`level\` in the last \`window\` bars?" as a running series.

**Formula.**
\`\`\`
ma  = SMA(close, ma_window)
tap = crossed_up(ma, level) OR crossed_down(ma, level)   # any crossing
out = count_in_window(tap, window)                       # rolling tap count
\`\`\`
Up- and down-crossings cannot both occur on the same bar, so the logical OR is computed as the element-wise \`max\` of the two cross masks (with \`NaN\` propagated).

**Parameters**
- \`level\` (float, default 100.0): the reference level whose crossings are counted. Set it to the price (or oscillator) level you care about.
- \`window\` (int, default 20): the trailing window, in bars, over which taps are counted. The first \`window - 1\` bars are \`NaN\` (no full window yet).
- \`ma_window\` (int, default 20): smoothing length of the simple moving average applied to \`close\` before counting crossings. Use \`1\` for no smoothing. Must be \`>= 1\`.

**How to use it.** There is deliberately no \`n\` parameter. To fire when price has tapped the level at least N times, drop NthTap into a signal as an \`IndicatorOperand\` and add \`Compare(ge N)\` — e.g. \`Compare(ge 3)\` means "tapped \`level\` at least 3 times in the last \`window\` bars". The same indicator instance serves every threshold.

**Edge cases**
- The first \`window - 1\` bars are \`NaN\` (incomplete trailing window); the moving-average warm-up adds further \`NaN\`s for the first \`ma_window - 1\` bars, and the cross helpers add a \`NaN\` at the first bar (no predecessor).
- If any bar inside the trailing window is \`NaN\`, the count is undefined → \`NaN\` for that bar (the rolling count never silently under-counts across a gap).
- A bar that merely *touches* \`level\` without strictly crossing it is not a tap; counting follows the strict cross semantics of \`crossed_up\` / \`crossed_down\`.`,
  ownPanel: true,
};
