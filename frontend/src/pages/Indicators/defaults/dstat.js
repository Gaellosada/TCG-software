// DStat (raw) — vol-normalised distance of price from its moving average.
// Layer-1 statistic from SPEC §4.1 (canonical legacy DStat, log-return /
// sample-stdev variant). Standalone raw line; the trailing-percentile line
// ships separately as ``dstat-percentile``.
const code = `def compute(series, ma_window: int = 21, vol_window: int = 63):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    # Warm-up per SPEC §4.1: first raw value at index max(ma_window, 2*vol_window).
    # DISCREPANCY (noted in PROBLEMS.md): the formula only needs ma_window closes
    # for MA and vol_window+1 closes for the vol_window log-returns, so index
    # max(ma_window-1, vol_window) would already be well-defined. The spec/legacy
    # (DStatStateHistory.java) use the more conservative 2*vol_window; implemented
    # per spec to match legacy warm-up exactly rather than silently deviate.
    warm = max(ma_window, 2 * vol_window)
    if ma_window < 1 or vol_window < 2 or n <= warm:
        return out
    for i in range(warm, n):
        ma = np.mean(s[i - ma_window + 1 : i + 1])
        chunk = s[i - vol_window : i + 1]                 # vol_window+1 closes
        rets = np.log(chunk[1:] / chunk[:-1])             # vol_window log-returns
        vol = np.std(rets, ddof=1) * (252.0 ** 0.5)       # annualised sample stdev
        if ma == 0.0 or vol == 0.0 or np.isnan(vol):
            continue
        out[i] = (s[i] / ma - 1.0) / vol
    return out`;

export default {
  id: 'dstat',
  name: 'DStat',
  readonly: true,
  category: 'statistical',
  compatibleAssetTypes: ['index', 'equity'],
  chartShape: 'time-series',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** DStat ("distance statistic") measures how far the current close sits above or below its own recent moving average, *normalised by the instrument's own realised volatility*. It answers "how many vol-units stretched is price versus its trend?" — a positive DStat means price is extended above its MA relative to how noisy it usually is; a negative DStat means it is stretched below. Because the denominator is the annualised realised vol, a +2 reading on a calm instrument and a +2 reading on a wild one are directly comparable. It is the Layer-1 statistic of the canonical legacy DStat regime engine (SPX / VIX / VVIX all use the same parameters).

**Formula.**
\`\`\`
r_t     = ln(close_t / close_{t-1})                       (log returns)
MA_t    = (1 / ma_window) * sum_{k=t-ma_window+1..t} close_k
vol_t   = std(r_{t-vol_window+1..t}, ddof=1) * sqrt(252)   (annualised sample stdev)
DSTAT_t = (close_t / MA_t - 1) / vol_t
\`\`\`
where \`std(..., ddof=1)\` is the Bessel-corrected sample standard deviation of the last \`vol_window\` **log** returns (denominator \`vol_window - 1\`), and the vol window consumes \`vol_window + 1\` closes.

**Parameters**
- \`ma_window\` (int, default 21): length of the simple moving average of close (~one trading month).
- \`vol_window\` (int, default 63): number of log-returns in the realised-vol window (~one trading quarter).

**Edge cases**
- Output is \`NaN\` for the first \`max(ma_window, 2*vol_window)\` bars. This warm-up matches the canonical legacy engine and is deliberately more conservative than the strict minimum the formula needs (\`max(ma_window - 1, vol_window)\`).
- If the vol window is perfectly flat (all identical closes ⇒ zero returns), \`vol_t = 0\`; that bar is left \`NaN\` (division would be undefined) rather than \`±inf\`.
- \`MA_t == 0\` (only possible with non-positive prices) leaves the bar \`NaN\`.
- A single \`NaN\` in close propagates through every MA and vol window that contains it.
- Uses **log** returns for the vol term (SPEC §4.1 canonical variant), unlike Historical Volatility which uses simple returns.`,
  ownPanel: true,
};
