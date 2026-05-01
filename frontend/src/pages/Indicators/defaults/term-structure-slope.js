// Term-Structure Slope — front-minus-back ATM-contract IV.
//
// Two-stream option-native default. Consumes ``series['front_atm_iv']`` and
// ``series['back_atm_iv']`` and returns their elementwise difference. Sign
// convention: positive => front-month richer than back-month (backwardated
// vol term structure, often associated with short-dated stress); negative
// => contango (typical regime).
//
// Both inputs are nearest-strike-to-spot ATM-CONTRACT IVs (per the
// ``atm-contract-iv`` honesty rename) — front uses ``offset_months: 0``
// and back uses ``offset_months: 1`` of the next-third-Friday rule, both
// with the same ``ByMoneyness(target=1.0, tolerance=0.05)`` selection.
// When the user pins the indicator without picking custom streams, the
// ``defaultSeries`` field below auto-binds both slots on OPT_SP_500.
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
      // Lock to monthlies — see atm-contract-iv.js for the rationale
      // (OPT_SP_500 mixes M and W cycles; "front month" only means the
      // SPX monthly).
      cycle: 'M',
      maturity: { kind: 'next_third_friday', offset_months: 0 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'iv',
    },
    back_atm_iv: {
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: 'M',
      maturity: { kind: 'next_third_friday', offset_months: 1 },
      selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
      stream: 'iv',
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
- Both default streams use the SAME selection (\`ByMoneyness(target=1.0, tolerance=0.05)\`) — only the \`offset_months\` differs (0 vs. 1). If the user repoints either slot at a different selection or different underlying, alignment of the slope semantics becomes their responsibility.`,
  ownPanel: true,
  chartMode: 'lines',
};
