/**
 * Per-bar OHLC validity check.
 * A bar is valid only if all four values are non-zero AND satisfy
 * the fundamental OHLC invariants:
 *   high >= max(open, close)
 *   low  <= min(open, close)
 *   high >= low
 *
 * Also rejects exact-marubozu bars where the body fills the entire range
 * with open at one extreme and close at the other (body === range).
 * These arise when zero-valued open+low (or open+high) fields are shifted
 * by a constant offset (difference adjustment), producing bars like
 * open=-1.35, high=22, low=-1.35, close=22.  Real market data almost
 * never produces exact IEEE-754 equality at both extremes simultaneously.
 */
export function isValidOHLC(o, h, l, c) {
  if (o === 0 || h === 0 || l === 0 || c === 0) return false;
  if (o == null || h == null || l == null || c == null) return false;
  if (h < Math.max(o, c)) return false;
  if (l > Math.min(o, c)) return false;
  if (h < l) return false;

  // Reject exact-marubozu: body fills the entire range with open at an extreme.
  const range = h - l;
  if (range > 0) {
    const body = Math.abs(o - c);
    if (body === range && (o === l || o === h)) return false;
  }

  return true;
}

/**
 * Prepare chart data from raw OHLC arrays.
 *
 * Returns:
 *   hasOHLC   — true if ≥50% of bars pass per-bar validity
 *   hasVolume — true if volume array exists with ≥5% non-zero bars
 *   open/high/low/close — cleaned arrays where invalid bars are null
 *                         (Plotly skips null entries in candlestick traces)
 */
export function prepareChartData(data) {
  const hasArrays = data.open && data.high && data.low && data.close;

  // Volume quality: need ≥5% non-zero bars
  const nonZeroVols = data.volume ? data.volume.filter((v) => v > 0).length : 0;
  const hasVolume = data.volume && nonZeroVols > data.volume.length * 0.05;

  if (!hasArrays) {
    return { hasOHLC: false, hasVolume, open: null, high: null, low: null, close: null };
  }

  const len = data.open.length;
  let validCount = 0;

  const open = new Array(len);
  const high = new Array(len);
  const low = new Array(len);
  const close = new Array(len);

  for (let i = 0; i < len; i++) {
    if (isValidOHLC(data.open[i], data.high[i], data.low[i], data.close[i])) {
      open[i] = data.open[i];
      high[i] = data.high[i];
      low[i] = data.low[i];
      close[i] = data.close[i];
      validCount++;
    } else {
      // Null out the entire bar — Plotly will skip it
      open[i] = null;
      high[i] = null;
      low[i] = null;
      close[i] = null;
    }
  }

  const hasOHLC = validCount > len * 0.5;

  return { hasOHLC, hasVolume, open, high, low, close };
}
