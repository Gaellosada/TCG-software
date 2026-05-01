// Average True Range — arithmetic-mean variant.
// NOTE: uses simple arithmetic mean of True Range over ``window``, NOT
// Wilder smoothing. See docs/indicators.md and docs/design-decisions.md.
const code = `def compute(series, window: int = 14):
    h = series['high']
    l = series['low']
    c = series['close']
    n = h.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n < window + 1:
        return out
    prev_close = np.concatenate((np.array([np.nan]), c[:-1]))
    hl = h - l
    hc = np.abs(h - prev_close)
    cl = np.abs(prev_close - l)
    tr = np.maximum(np.maximum(hl, hc), cl)
    # tr[0] is NaN because prev_close[0] is NaN.
    # Arithmetic mean over 'window' TRs (not Wilder smoothing).
    csum = np.concatenate((np.array([0.0]), np.cumsum(tr[1:])))
    idx = np.arange(window, n)
    win_sum = csum[idx] - csum[idx - window]
    out[window:] = win_sum / window
    return out`;

export default {
  id: 'atr',
  name: 'Average True Range (ATR)',
  readonly: true,
  category: 'volatility',
  code,
  params: {},
  seriesMap: {},
  doc: `> ⚠️ **Requires OHLC data.** This indicator reads the **high, low, and close** series. Some datasets in this platform only contain close / adjusted-close — if yours does, this indicator will fail or return all-NaN. Check your source before selecting this indicator.

**Intuition.** ATR measures the average per-bar range traders can expect, taking overnight gaps into account via the True Range. It is used to size stops (\`k * ATR\`), to normalise breakout thresholds across instruments, and as a raw volatility proxy. Higher ATR means the instrument is moving more per bar.

⚠️ **Note.** This ATR uses a **simple arithmetic mean** of True Range values over the window, **NOT Wilder's recursive smoothing** used by the textbook ATR you may find in references. Practical difference: this variant reacts slightly faster to recent TR and has no infinite tail — each TR drops out of the window after \`window\` bars.

**Formula.**
\`\`\`
TR_t  = max( high_t - low_t,  |high_t - close_{t-1}|,  |close_{t-1} - low_t| )
ATR_t = (1 / window) * sum_{k = t - window + 1}^{t} TR_k
\`\`\`

**Parameters**
- \`window\` (int, default 14): number of True-Range bars in the rolling arithmetic mean. Larger values smooth more and react more slowly; 14 is the Wilder (1978) default.

**Edge cases**
- Output is \`NaN\` for the first \`window\` bars (bar 0 has no \`close_{t-1}\`, so TR only starts at index 1; ATR then needs \`window\` TRs).
- A flat bar (\`high == low == close_{t-1}\`) produces \`TR = 0\`; ATR can legitimately be 0.
- A \`NaN\` in any of high / low / close propagates into TR and contaminates the next \`window\` ATR values.
- Uses **population arithmetic mean** (denominator \`window\`), not Wilder recursive smoothing (see note above).`,
  ownPanel: true,
};
