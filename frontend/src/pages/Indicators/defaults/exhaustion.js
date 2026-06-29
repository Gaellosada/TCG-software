// Exhaustion — signed symmetric cascade detector.
// Detects a two-step "cascade" of the smoothed close through two levels
// within a rolling window, and emits a signed event: -1 on a DOWN cascade
// (cross down through `upper` then down through `lower`), +1 on the mirror
// UP cascade (cross up through `lower` then up through `upper`), 0 otherwise,
// NaN in warm-up / gaps. The two cascades are mutually exclusive event types.
// An in-progress down cascade is ABORTED if the series reclaims `upper`
// (cross up); a mirror abort (loses `lower`) cancels an up cascade.
// Consume one direction with Compare(eq 1) or Compare(eq -1) in a signal.
const code = `def compute(series, upper: float = 70.0, lower: float = 30.0, window: int = 10, ma_window: int = 20):
    assert upper > lower, 'upper must be strictly greater than lower'
    assert window >= 1, 'window must be >= 1'
    assert ma_window >= 1, 'ma_window must be >= 1'
    s = series['close']
    n = s.shape[0]
    ma = np.full(n, np.nan, dtype=float)
    if n >= ma_window:
        ma[ma_window - 1:] = np.convolve(s, np.ones(ma_window) / ma_window, mode='valid')
    down = ta.sequence_within(
        [ta.crossed_down(ma, upper), ta.crossed_down(ma, lower)],
        window,
        abort=ta.crossed_up(ma, upper),
    )
    up = ta.sequence_within(
        [ta.crossed_up(ma, lower), ta.crossed_up(ma, upper)],
        window,
        abort=ta.crossed_down(ma, lower),
    )
    out = np.where(
        np.isnan(down) | np.isnan(up),
        np.nan,
        (up == 1.0).astype(float) - (down == 1.0).astype(float),
    )
    return out`;

export default {
  id: 'exhaustion',
  name: 'Exhaustion',
  readonly: true,
  category: 'pattern',
  compatibleAssetTypes: ['index', 'equity'],
  chartShape: 'time-series',
  code,
  params: {},
  seriesMap: {},
  chartMode: 'markers',
  doc: `**Intuition.** Exhaustion looks for a momentum *cascade*: the smoothed price tearing through two reference levels in the same direction inside a short window, the way an over-extended move accelerates before it exhausts. A **down cascade** is the moving average crossing *down* through \`upper\` and then *down* through \`lower\` within \`window\` bars — emitted as **-1**. The mirror **up cascade** (up through \`lower\` then up through \`upper\`) is emitted as **+1**. Everything else is \`0\`. If a started down cascade reclaims \`upper\` before completing (an *abort*), the candidate is cancelled and no event fires; an up cascade aborts symmetrically when it loses \`lower\`.

**Formula.**
\`\`\`
ma   = SMA(close, ma_window)
down = sequence_within([crossed_down(ma, upper), crossed_down(ma, lower)], window,
                       abort = crossed_up(ma, upper))
up   = sequence_within([crossed_up(ma, lower),   crossed_up(ma, upper)],   window,
                       abort = crossed_down(ma, lower))
out  = +1 where up completes, -1 where down completes, 0 otherwise; NaN in warm-up/gaps
\`\`\`
The two event types are mutually exclusive (a single bar cannot complete both an up and a down cascade), so the signed output is always one of \`{-1, 0, +1}\`.

**Parameters**
- \`upper\` (float, default 70.0): the higher reference level. Must be strictly greater than \`lower\` — the indicator raises otherwise.
- \`lower\` (float, default 30.0): the lower reference level.
- \`window\` (int, default 10): maximum number of bars the cascade may take from the first cross to the second; longer gaps expire the candidate with no event. Must be \`>= 1\`.
- \`ma_window\` (int, default 20): smoothing length of the simple moving average applied to \`close\` before the cross detection. Use \`1\` for no smoothing. Must be \`>= 1\`.

**How to use it.** Drop Exhaustion into a signal as an \`IndicatorOperand\` and gate the direction with a \`Compare\`: \`Compare(eq 1)\` fires on the up cascade, \`Compare(eq -1)\` on the down cascade. The default \`upper\`/\`lower\` (70 / 30) suit an oscillator-scaled input; for a raw price series set them to price levels that bracket the move you care about.

**Edge cases**
- The first \`ma_window - 1\` bars are \`NaN\` (moving-average warm-up); the cross helpers add a further \`NaN\` at the first bar (no predecessor to compare against).
- Any \`NaN\` inside the smoothing window or at a bar whose cross status is being evaluated yields \`NaN\` at that bar and invalidates any in-progress candidate — the indicator never emits a spurious event across a gap.
- A reclaim of \`upper\` (or loss of \`lower\`) aborts the in-progress cascade; a fresh cascade can still start later from a new first cross.
- \`upper <= lower\` is ill-posed and raises immediately rather than silently producing garbage.`,
  ownPanel: true,
};
