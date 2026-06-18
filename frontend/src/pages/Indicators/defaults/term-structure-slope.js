// Term-Structure Slope — front-minus-back ATM-contract IV.
//
// Two-stream option-native default. Consumes ``series['front_atm_iv']`` and
// ``series['back_atm_iv']`` and returns their elementwise difference. Sign
// convention: positive => front-month richer than back-month (backwardated
// vol term structure, often associated with short-dated stress); negative
// => contango (typical regime).
//
// Both inputs are nearest-strike-to-spot ATM-CONTRACT IVs (per the
// ``atm-contract-iv`` honesty rename) — front uses ``nearest_to_target(30)``
// targeting ~1-month DTE and back uses ``nearest_to_target(90)`` targeting
// ~3-month DTE, both with the same ``ByMoneyness(target=1.0, tolerance=0.05)``
// selection. The DTE targets guarantee the two legs resolve to different
// expirations on any date.
const code = `def compute(series):
    front = series['front_atm_iv']
    back = series['back_atm_iv']
    return (front - back).astype(float)`;

export default {
  id: 'term-structure-slope',
  name: 'Term-Structure Slope',
  readonly: true,
  category: 'volatility',
  compatibleAssetTypes: ['option'],
  chartShape: 'time-series',
  code,
  params: {},
  seriesMap: {},
  // Auto-bind shape for ``hydrateDefault.applyDefaultSeries`` (Wave 2c).
  // One OptionStreamRef per series label parsed from ``code``. Both slots
  // default to OPT_SP_500 ATM-contract IV streams; only the maturity
  // offset differs (front-month vs. back-month).
  defaultSeries: {
    front_atm_iv: {
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      // Lock to W3 Friday — the real "monthly" cycle for SPX. IVolatility
      // tags quarterly (Mar/Jun/Sep/Dec) third-Friday contracts as 'M' and
      // all other months' third-Friday contracts as 'W3 Friday'. Using 'M'
      // would leave 8 of 12 months with no data. 'W3 Friday' covers every
      // month's third Friday (PM-settled SPXW contracts).
      cycle: 'W3 Friday',
      maturity: { kind: 'nearest_to_target', target_days: 30 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'iv',
      adjustment: 'none',
      roll_offset: 0,
    },
    back_atm_iv: {
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      // Same W3 Friday cycle rationale as front_atm_iv above.
      cycle: 'W3 Friday',
      maturity: { kind: 'nearest_to_target', target_days: 90 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'iv',
      adjustment: 'none',
      roll_offset: 0,
    },
  },
  doc: `**Intuition.** The term-structure slope of implied volatility compares the front-expiry ATM-contract IV against a longer-dated (back) expiry ATM-contract IV. In normal regimes, longer-dated IV exceeds front-month IV (contango, slope < 0): the market demands a vol premium for time. Around stress events the front explodes and the slope flips positive (backwardation): short-dated options are richer than long-dated ones. Traders use the sign and magnitude of the slope as a stress / regime signal and as a calendar-spread entry filter.

**Formula.**
\`\`\`
slope_t = front_atm_iv_t - back_atm_iv_t
\`\`\`
The output preserves the input units (volatility points). No smoothing, no scaling.

**Parameters**
- None. Both streams are supplied via \`series\`; the user picks which expiries are front and back when wiring the indicator.

**Edge cases**
- A \`NaN\` in either input on a given bar produces \`NaN\` on that bar (no propagation, no fill).
- Inputs must be the same length and aligned on the same time index — that is the consumer's job. The indicator does NOT validate alignment.
- Sign convention: positive means front > back (backwardated). Negative means front < back (contango). Zero means flat.
- The two streams should both be ATM-contract IV series of the SAME underlying; mixing underlyings or non-ATM strikes makes the slope meaningless.
- Both default streams use the SAME selection (\`ByMoneyness(target=1.0, tolerance=0.05)\`) — front targets ~30 DTE via \`nearest_to_target(30)\` and back targets ~90 DTE via \`nearest_to_target(90)\`. If the user repoints either slot at a different selection or different underlying, alignment of the slope semantics becomes their responsibility.`,
  ownPanel: true,
  chartMode: 'lines',
};
