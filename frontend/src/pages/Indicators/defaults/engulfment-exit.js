// Engulfment TP/SL Exit — paired position-exit monitor for engulfment-pattern.
// User supplies a paired ``entry`` series: entry_t = signed breakout level at
// entry bars (as emitted by ``engulfment-pattern``), NaN otherwise. The
// indicator re-derives the engulfment-box height by looking back to the
// consolidation that preceded each entry (as a rolling high-minus-low over
// ``box_lookback`` bars ending just before the entry), then watches
// subsequent OHLC bars until TP or SL is hit. Emits the exit price on the
// hit bar and NaN on all other bars.
const code = `def compute(series, box_lookback: int = 20, ratio_win: float = 2.0, ratio_loss: float = 1.0):
    o = series['open']
    h = series['high']
    l = series['low']
    e = series['entry']
    n = h.shape[0]
    out = np.full(n, np.nan, dtype=float)

    entry_price = np.nan
    direction = 0          # +1 up, -1 down
    tp = np.nan
    sl = np.nan

    for t in range(n):
        et = e[t] if t < e.shape[0] else np.nan
        # New entry on this bar (signed level as emitted by engulfment-pattern).
        if not np.isnan(et) and et != 0.0:
            # Estimate box height from the last 'box_lookback' pre-entry bars.
            start = max(0, t - box_lookback)
            if start < t:
                w_h = h[start:t]
                w_l = l[start:t]
                box = float(np.nanmax(w_h) - np.nanmin(w_l))
            else:
                box = 0.0
            direction = 1 if et > 0 else -1
            entry_price = abs(et)
            tp = entry_price + direction * ratio_win * box
            sl = entry_price - direction * ratio_loss * box
            # Do not exit on the entry bar itself.
            continue

        if direction == 0:
            continue

        # Watch for exit on this bar.
        if direction == 1:
            # Long: TP is above entry, SL is below.
            if o[t] > tp or o[t] < sl:
                out[t] = o[t]
                direction = 0
            elif h[t] > tp:
                out[t] = tp
                direction = 0
            elif l[t] < sl:
                out[t] = sl
                direction = 0
        else:
            # Short: TP is below entry, SL is above.
            if o[t] < tp or o[t] > sl:
                out[t] = o[t]
                direction = 0
            elif l[t] < tp:
                out[t] = tp
                direction = 0
            elif h[t] > sl:
                out[t] = sl
                direction = 0

    return out`;

export default {
  id: 'engulfment-exit',
  name: 'Engulfment TP/SL Exit',
  readonly: true,
  category: 'pattern',
  code,
  params: {},
  seriesMap: {},
  doc: `> ⚠️ **Requires OHLC data.** This indicator reads the **open, high, and low** series (plus the paired \`entry\` stream). Some datasets in this platform only contain close / adjusted-close — if yours does, this indicator will fail or return all-NaN. Check your source before selecting this indicator.

**Intuition.** A stateful position-exit monitor paired with \`engulfment-pattern\`. Each time the paired entry series reports a signed breakout level, this indicator anchors a take-profit level at \`entry + r_win * box_height\` and a stop-loss level at \`entry - r_loss * box_height\`. It then watches each subsequent bar's OHLC until either level is hit, at which point it emits the fill price on that bar and waits for the next entry. Used to close engulfment-breakout trades with a fixed reward:risk ratio.

**Formula.**
\`\`\`
box_height_t = max(high) - min(low) over the preceding 'box_lookback' bars
direction    = sign(entry_t)                (+1 long, -1 short)
TP           = |entry| + direction * r_win  * box_height
SL           = |entry| - direction * r_loss * box_height

For each bar t after the entry, until an exit is emitted:
    long  (d = +1):  gap  → exit = open_t    if open_t > TP or open_t < SL
                     TP   → exit = TP        else if high_t > TP
                     SL   → exit = SL        else if low_t  < SL
    short (d = -1):  symmetric with H/L swapped.
\`\`\`

**Parameters**
- \`box_lookback\` (int, default 20): number of bars before each entry used to estimate the engulfment-box height. For the default \`min_engulfing_periods = 5\` in \`engulfment-pattern\`, 20 covers roughly four consolidation cycles before the entry. Tune up if you lengthen \`engulfment-pattern\`'s window.
- \`ratio_win\` (float, default 2.0): TP distance as a multiple of the box height. Higher values yield larger R:R but fewer winners.
- \`ratio_loss\` (float, default 1.0): SL distance as a multiple of the box height. Lower values tighten the stop.

**Inputs**
- \`series['open']\`, \`series['high']\`, \`series['low']\` — price OHLC.
- \`series['entry']\` — paired signed-breakout stream from \`engulfment-pattern\`. Must have the same length as OHLC. \`NaN\` on non-entry bars, positive number on up-breakouts, negative number on down-breakouts.

**Edge cases**
- Before any entry arrives, or between a fill and the next entry, output is \`NaN\`.
- A gap bar whose open has already passed TP or SL emits the open as the fill price (conservative — you can't realistically fill mid-gap at the level).
- Both TP and SL hittable within the same bar: the order of check is \`open → high (TP for long / SL for short) → low (SL for long / TP for short)\`, which favours TP-on-long and TP-on-short.
- \`NaN\` in OHLC during an open position contaminates the max/min box estimate and can lead to unexpected levels; clean upstream.
- The paired \`entry\` series must be aligned bar-for-bar with OHLC; any misalignment will shift or miss exits.

**Notes on composition.** Entry parameters are supplied as an explicit \`entry\` input series (paired with \`engulfment-pattern\`) plus a \`box_lookback\` estimate, which fits the sandbox contract \`compute(series, ...)\`. The TP / SL fill logic is preserved faithfully.`,
  ownPanel: false,
};
