// Term-Structure Slope — front-minus-back ATM IV.
//
// Two-stream option-native default. Consumes ``series['front_atm_iv']`` and
// ``series['back_atm_iv']`` and returns their elementwise difference. Sign
// convention: positive => front-month richer than back-month (backwardated
// vol term structure, often associated with short-dated stress); negative
// => contango (typical regime).
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
  doc: `**Intuition.** The term-structure slope of implied volatility compares the front-expiry ATM IV against a longer-dated (back) expiry ATM IV. In normal regimes, longer-dated IV exceeds front-month IV (contango, slope < 0): the market demands a vol premium for time. Around stress events the front explodes and the slope flips positive (backwardation): short-dated options are richer than long-dated ones. Traders use the sign and magnitude of the slope as a stress / regime signal and as a calendar-spread entry filter.

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
- The two streams should both be ATM IV series of the SAME underlying; mixing underlyings or non-ATM strikes makes the slope meaningless.`,
  ownPanel: true,
  chartMode: 'lines',
};
