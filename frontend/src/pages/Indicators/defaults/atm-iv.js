// ATM IV — front-expiry at-the-money implied volatility, optionally smoothed.
//
// This is an option-native default: it consumes the semantic series label
// ``atm_iv`` rather than ``close``. The matching SeriesRef variant that
// auto-binds this label is a v2 task; for v1 the user wires the slot
// manually after picking an option-derived ATM IV stream.
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
  id: 'atm-iv',
  name: 'ATM IV',
  readonly: true,
  category: 'volatility',
  compatibleAssetTypes: ['option'],
  chartShape: 'time-series',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** ATM IV is the front-expiry at-the-money implied volatility, the market's forward-looking estimate of how much the underlying will move (annualised, in volatility points). It is the single most-watched option-market signal: it captures how much premium the market is charging for ATM options on the nearest listed expiry. Traders use it to gauge fear / complacency, to compare against realised volatility (vol risk premium), and as a regime filter. This indicator is a pass-through (or optionally a simple-moving-average smooth) over the supplied ATM IV time series.

**Formula.**
\`\`\`
out_t = atm_iv_t                                  if smoothing_window <= 1
out_t = (1 / w) * sum_{k = t - w + 1}^{t} atm_iv_k  if smoothing_window = w >= 2
\`\`\`
With \`w >= 2\`, the first \`w - 1\` outputs are \`NaN\` (warm-up); subsequent outputs are the equal-weight rolling mean of the last \`w\` ATM IV observations.

**Parameters**
- \`smoothing_window\` (int, default 1): SMA window applied on top of the raw ATM IV series. Default of 1 disables smoothing (pass-through). Larger values smooth more but add lag.

**Edge cases**
- With \`smoothing_window = 1\`, the indicator is exactly the input series cast to float64 — no NaN warm-up and no lag.
- With \`smoothing_window > n\`, the entire output is \`NaN\`.
- A single \`NaN\` in the ATM IV input poisons every output bar whose smoothing window contains it (for \`smoothing_window\` consecutive bars).
- The indicator does not unit-convert — it returns ATM IV in whatever units the upstream stream emits (typically annualised vol in % or in decimal). Match the consumer's expectation accordingly.`,
  ownPanel: true,
  chartMode: 'lines',
};
