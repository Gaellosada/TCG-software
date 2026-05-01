// Swing Pivots — confirmed local extrema with inflection lag.
// Emits the price level at confirmed swing highs AND swing lows as a
// POSITIVE value; NaN elsewhere. The chart renders this indicator as
// a zigzag line (connectgaps=true on the indicator trace) so consecutive
// confirmed pivots are joined across the NaN bars between them — the
// line visually passes through the price at each pivot bar.
const code = `def compute(series, total_periods: int = 20, inflection_periods: int = 5):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if inflection_periods > total_periods or n < total_periods or inflection_periods < 1:
        return out

    last_min = None
    last_max = None

    # For each bar t with t >= total_periods - 1, the candidate pivot is
    # the bar at position (t - inflection_periods), inside a total-periods
    # window ending at t.
    for t in range(total_periods - 1, n):
        cand_idx = t - inflection_periods
        if cand_idx < 0:
            continue
        w = s[t - total_periods + 1 : t + 1]
        if np.any(np.isnan(w)):
            continue
        cand_value = s[cand_idx]
        win_min = np.min(w)
        win_max = np.max(w)
        # doubleEquals tolerance — use exact-equality here since comparisons
        # are over unchanged float values (no accumulated error).
        if cand_value == win_min and (last_min is None or win_min < last_min):
            out[cand_idx] = win_min
            last_min = win_min
            last_max = None
        elif cand_value == win_max and (last_max is None or win_max > last_max):
            out[cand_idx] = win_max
            last_max = win_max
            last_min = None

    return out`;

export default {
  id: 'swing-pivots',
  name: 'Swing Pivots',
  readonly: true,
  category: 'pattern',
  code,
  params: {},
  seriesMap: {},
  doc: `⚠️ **Note.** This detects **pivot inflections** (confirmed swing highs and swing lows with a confirmation delay), **NOT** a rolling high/low envelope. If you want price-range bands (upper = rolling max of highs, lower = rolling min of lows, à la Donchian Channel), use \`trailing-extreme\` with the appropriate mode instead.

**Intuition.** Detects confirmed swing highs and swing lows (local extrema) in close using two windows: a larger \`total_periods\` window used to define the extreme, and a smaller \`inflection_periods\` window used as a confirmation delay. A swing high is confirmed when the bar \`inflection_periods\` ago was the maximum of the enclosing \`total_periods\` window AND is a new high since the last confirmed swing low. Swing lows are symmetric. Practitioners use these pivots for support / resistance levels, Elliott-wave labelling, and zig-zag chart overlays. Because pivots are confirmed with a lag, outputs are always stamped at the pivot bar itself (not the confirmation bar).

**Formula.**
\`\`\`
For each bar t with t >= total_periods - 1:
    cand_idx = t - inflection_periods
    W        = close[t - total_periods + 1 .. t]
    cand     = close[cand_idx]
    if cand == min(W) and (last_min is None or min(W) < last_min):
        emit swing_low at cand_idx;  last_min = min(W); last_max = None
    elif cand == max(W) and (last_max is None or max(W) > last_max):
        emit swing_high at cand_idx; last_max = max(W); last_min = None
\`\`\`
The \`last_max = None\` reset after a low (and vice versa) enforces the alternation "high, then low, then high, ...".

**Parameters**
- \`total_periods\` (int, default 20): window over which the extreme must hold.
- \`inflection_periods\` (int, default 5): confirmation delay. Must satisfy \`1 <= inflection_periods <= total_periods\`.

**Output encoding.** Both swing highs and swing lows are emitted as the raw **positive** price level at the pivot bar; non-pivot bars are \`NaN\`. Direction is not encoded in the sign — distinguish a high from a low by position (a point above nearby prices is a high, below is a low) or by cross-referencing with the close trace. On the chart, pivots render as a zigzag line connecting consecutive swing highs and lows — the line visually passes through the price at each confirmed pivot bar.

**Edge cases**
- Output is \`NaN\` on all non-pivot bars. The first bar at which a pivot can be emitted is index \`total_periods - 1 - inflection_periods\` — that is the first bar where \`t >= total_periods - 1\` and thus \`cand_idx = t - inflection_periods\` lies inside a full \`total_periods\`-length window.
- \`NaN\` inside the window disqualifies that window (no pivot emitted).
- Alternation is strict: two consecutive highs cannot be emitted without an intervening low. The first pivot after the warm-up can be either direction depending on whether the first eligible candidate is the window min or max.
- Equality comparisons use exact equality on the observed floats; no numerical-tolerance check is applied (the candidate value is the same object being compared to \`min(W)\` / \`max(W)\`).`,
  ownPanel: false,
};
