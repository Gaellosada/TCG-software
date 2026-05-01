// ATM Contract IV — front-month *nearest-strike-to-spot contract's* stored
// implied volatility, optionally smoothed.
//
// Honest naming note: this indicator picks the *nearest-strike-to-spot
// front-month contract* and surfaces that single contract's stored IV
// (NOT interpolated ATM IV at K = S, which would require a smile fit and
// is reserved for a future ``atm-iv-interpolated`` variant).
//
// This is an option-native default: it consumes the semantic series label
// ``atm_iv`` rather than ``close``. The matching SeriesRef variant that
// auto-binds this label is the new ``OptionStreamRef`` (Wave 2a). When
// the user pins the indicator without picking a custom stream, the
// ``defaultSeries`` field below auto-binds the slot to OPT_SP_500 with
// a ByMoneyness(target=1.0, tolerance=0.05) ATM selection on the
// next-third-Friday front month.
const code = `def compute(series, smoothing_window: int = 1):
    s = series['atm_iv']
    n = s.shape[0]
    if smoothing_window <= 1:
        return s.astype(float)
    out = np.full(n, np.nan, dtype=float)
    if n < smoothing_window:
        return out
    out[smoothing_window - 1:] = np.convolve(
        s, np.ones(smoothing_window) / smoothing_window, mode='valid'
    )
    return out`;

export default {
  id: 'atm-contract-iv',
  name: 'ATM contract IV',
  readonly: true,
  category: 'volatility',
  compatibleAssetTypes: ['option'],
  chartShape: 'time-series',
  code,
  params: {},
  seriesMap: {},
  // Auto-bind shape for ``hydrateDefault.applyDefaultSeries`` (Wave 2c).
  // One OptionStreamRef per series label parsed from ``code``. Picks the
  // *nearest-strike-to-spot front-month contract's* stored IV on
  // OPT_SP_500 — the honest "ATM contract IV" semantic.
  defaultSeries: {
    atm_iv: {
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: null,
      maturity: { kind: 'next_third_friday', offset_months: 0 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'iv',
    },
  },
  doc: `**Intuition.** ATM contract IV is the stored implied volatility of the *nearest-strike-to-spot front-month contract* — i.e. the single listed contract whose strike is closest to current spot, on the next listed monthly expiry. It is the cheapest-to-compute proxy for "the ATM IV the market is showing right now": no smile fit, no interpolation, just the IV the data provider stamped on that one contract. Traders use it to gauge fear / complacency, to compare against realised volatility (vol risk premium), and as a regime filter. This indicator is a pass-through (or optionally a simple-moving-average smooth) over the supplied stream.

**Honest naming.** Academically, "ATM IV" usually refers to the *interpolated* IV at exactly K = S, which requires a smile fit at every date. This indicator does NOT do that — it surfaces the nearest-strike-to-spot front-month contract's stored IV. A future \`atm-iv-interpolated\` variant will provide the interpolated definition; the two are distinct and the rename to \`atm-contract-iv\` makes that explicit.

**Formula.**
\`\`\`
out_t = atm_iv_t                                  if smoothing_window <= 1
out_t = (1 / w) * sum_{k = t - w + 1}^{t} atm_iv_k  if smoothing_window = w >= 2
\`\`\`
With \`w >= 2\`, the first \`w - 1\` outputs are \`NaN\` (warm-up); subsequent outputs are the equal-weight rolling mean of the last \`w\` ATM IV observations.

**Parameters**
- \`smoothing_window\` (int, default 1): SMA window applied on top of the raw stream. Default of 1 disables smoothing (pass-through). Larger values smooth more but add lag.

**Edge cases**
- With \`smoothing_window = 1\`, the indicator is exactly the input series cast to float64 — no NaN warm-up and no lag.
- With \`smoothing_window > n\`, the entire output is \`NaN\`.
- A single \`NaN\` in the stream poisons every output bar whose smoothing window contains it (for \`smoothing_window\` consecutive bars).
- The indicator does not unit-convert — it returns IV in whatever units the upstream stream emits (typically annualised vol in % or in decimal). Match the consumer's expectation accordingly.`,
  ownPanel: true,
  chartMode: 'lines',
};
