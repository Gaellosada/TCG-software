// Engulfment Pattern — rolling range-breakout detector.
// State machine emitting the breakout price level (POSITIVE) on the bar
// the breakout occurs; NaN otherwise.
// The chart renders this as a zigzag line (connectgaps=true on the
// indicator trace) overlaid on price, so sparse breakout points are
// joined across the NaN bars between them and remain visible against
// the price trace. Direction (bullish / bearish) is NOT encoded in the
// sign: users cross-reference the point's position against the candle
// to see if it hit the high side or the low side.
const code = `def compute(series, min_engulfing_periods: int = 5):
    o = series['open']
    h = series['high']
    l = series['low']
    n = h.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < min_engulfing_periods:
        return out

    # Seed engulfing box from the first 'min_engulfing_periods' bars,
    # resetting if any bar expands the range before the minimum is reached.
    eng_high = h[0]
    eng_low = l[0]
    current = 0  # bars included so far (post any reset)
    for i in range(1, min_engulfing_periods):
        if h[i] > eng_high or l[i] < eng_low:
            eng_high = h[i]
            eng_low = l[i]
            current = 0
        else:
            current += 1

    for t in range(min_engulfing_periods, n):
        if current >= min_engulfing_periods:
            if h[t] <= eng_high and l[t] >= eng_low:
                current += 1
                # still contained, no breakout emitted
            else:
                current = 0
                if h[t] > eng_high and l[t] < eng_low:
                    # Both broken same bar — pick level closer to open.
                    if abs(h[t] - o[t]) < abs(o[t] - l[t]):
                        out[t] = eng_high
                    else:
                        out[t] = eng_low
                elif h[t] > eng_high:
                    # Upside breakout: level = max(open, eng_high)
                    lvl = o[t] if o[t] > eng_high else eng_high
                    out[t] = lvl
                else:
                    # Downside breakout: level = min(open, eng_low)
                    lvl = o[t] if o[t] < eng_low else eng_low
                    out[t] = lvl
                eng_high = h[t]
                eng_low = l[t]
        else:
            if h[t] > eng_high or l[t] < eng_low:
                eng_high = h[t]
                eng_low = l[t]
                current = 0
            else:
                current += 1

    return out`;

export default {
  id: 'engulfment-pattern',
  name: 'Engulfment Pattern',
  readonly: true,
  category: 'pattern',
  code,
  params: {},
  seriesMap: {},
  doc: `> ⚠️ **Requires OHLC data.** This indicator reads the **open, high, and low** series in addition to close-derived state. Some datasets in this platform only contain close / adjusted-close — if yours does, this indicator will fail or return all-NaN. Check your source before selecting this indicator.

**Intuition.** Tracks a rolling consolidation "engulfment box" — the highest high and lowest low over a stretch of bars during which neither extreme is broken. When a subsequent bar pierces the box on the high side or the low side, the indicator emits the breakout price level on that bar and starts tracking a new box from the breakout bar's range. Practitioners use it as a range-breakout entry signal: long consolidations often precede larger directional moves.

**Formula.**
\`\`\`
Maintain (eng_high, eng_low, current_count).
During seeding (first min_engulfing_periods bars):
    if high_t > eng_high or low_t < eng_low:
        reset (eng_high, eng_low) = (high_t, low_t); current_count = 0
    else:
        current_count += 1

After seeding, at each bar t:
    if current_count >= min_engulfing_periods:
        if high_t <= eng_high and low_t >= eng_low:
            current_count += 1              # still inside, emit NaN
        else:
            determine breakout level (open, eng_high, eng_low, high_t, low_t)
            reset box to (high_t, low_t); current_count = 0
            emit level as a positive price
\`\`\`
Both-sides-in-one-bar tiebreak: pick the level closer to the open
(\`|high_t - open_t| < |open_t - low_t|\` → upside; else downside).

**Parameters**
- \`min_engulfing_periods\` (int, default 5): minimum consolidation length before breakouts are reported. Shorter values yield more and noisier signals.

**Output encoding.** Both upside and downside breakouts are emitted as the raw **positive** price level at the breakout bar (upside = \`max(open_t, eng_high)\`, downside = \`min(open_t, eng_low)\`); non-breakout bars are \`NaN\`. Direction is **not** encoded in the sign — to distinguish a bullish from a bearish breakout, cross-reference the point's position against the candle (points sitting near the candle high flag an upside break; near the low, a downside break). On the chart, breakout levels render as a zigzag line overlaid on price — consecutive breakouts are joined across the NaN bars between them so the sparse output is visible.

**Edge cases**
- Output is \`NaN\` on non-breakout bars. Consumers can \`~np.isnan()\` to detect events.
- A truly flat series that never breaks will never emit a signal.
- \`NaN\` in the input corrupts the state machine for subsequent bars; clean upstream.

**Notes on output contract.** The underlying semantics emit two quantities (breakout level + signed period count) and previously folded direction into the sign of the level. That produced negative price values which rendered off-chart; the current contract emits only the positive level and relies on the zigzag line overlay for visibility.`,
  ownPanel: false,
};
