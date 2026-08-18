// DStat percentile line — trailing nearest-rank percentile of raw DStat.
// Layer-2 of SPEC §4.1: recompute raw DStat internally (Layer-1, self-
// contained), then emit the value at the ``percentile``-th nearest rank of
// the trailing ``pct_window`` raw values. "DSTAT hits 95%" ≡ raw DStat rising
// above this line with percentile=95.
const code = `def compute(series, ma_window: int = 21, vol_window: int = 63, pct_window: int = 1260, percentile: float = 95.0):
    s = series['close']
    n = s.shape[0]
    out = np.full(n, np.nan, dtype=float)
    warm = max(ma_window, 2 * vol_window)
    if ma_window < 1 or vol_window < 2 or pct_window < 1:
        return out
    if percentile < 0.0 or percentile > 100.0 or n <= warm:
        return out
    # --- Layer 1: raw DStat (identical math to the dstat default) ---
    raw = np.full(n, np.nan, dtype=float)
    for i in range(warm, n):
        ma = np.mean(s[i - ma_window + 1 : i + 1])
        chunk = s[i - vol_window : i + 1]
        rets = np.log(chunk[1:] / chunk[:-1])
        vol = np.std(rets, ddof=1) * (252.0 ** 0.5)
        if ma == 0.0 or vol == 0.0 or np.isnan(vol):
            continue
        raw[i] = (s[i] / ma - 1.0) / vol
    # --- Layer 2: nearest-rank percentile over the trailing pct_window raws ---
    # idx = ceil(p * N) - 1 (1-based nearest rank mapped to 0-based index),
    # clamped to [0, pct_window-1] to stay in range for the p=0 / p=100 corners.
    p = percentile / 100.0
    idx = int(np.ceil(p * pct_window)) - 1
    if idx < 0:
        idx = 0
    if idx >= pct_window:
        idx = pct_window - 1
    first_valid = warm + pct_window - 1
    for t in range(first_valid, n):
        w = raw[t - pct_window + 1 : t + 1]
        w_clean = w[~np.isnan(w)]
        if w_clean.shape[0] < pct_window:
            continue                                     # incomplete window -> NaN
        out[t] = np.sort(w_clean)[idx]
    return out`;

export default {
  id: 'dstat-percentile',
  name: 'DStat Percentile',
  readonly: true,
  category: 'statistical',
  compatibleAssetTypes: ['index', 'equity'],
  chartShape: 'time-series',
  code,
  params: {},
  seriesMap: {},
  doc: `**Intuition.** This is the trailing *reference line* for DStat (SPEC §4.1 Layer 2). It first recomputes the raw DStat statistic internally (self-contained — no dependency on the \`dstat\` cell), then reports where the \`percentile\`-th rank of the last \`pct_window\` raw DStat values sits. With \`percentile = 95\` the line traces DStat's own trailing 95th percentile, so a regime rule like *"DSTAT hits 95%"* is simply *raw DStat rising above this line*. Instantiate copies at 10 / 20 / 50 / 60 / 75 / 95 to build the hysteresis bands the strategies use.

> ⚠️ **\`percentile\` is on a 0..100 scale, NOT a 0..1 fraction.** Default \`percentile = 95.0\` = the 95th percentile. Values outside \`[0, 100]\` produce all-NaN output.

**Formula.**
\`\`\`
raw_t = DStat(close; ma_window, vol_window)               (Layer 1, see the DStat default)
p     = percentile / 100
idx   = ceil(p * pct_window) - 1                           (nearest-rank, 0-based; clamped)
W_t   = sort({ raw_{t-pct_window+1}, ..., raw_t })         (ascending)
out_t = W_t[idx]
\`\`\`
Nearest-rank means the emitted value is always an actually-observed raw DStat, never interpolated. Example: \`pct_window = 1260\`, \`percentile = 95\` ⇒ \`idx = ceil(0.95 * 1260) - 1 = 1197 - 1 = 1196\`.

**Parameters**
- \`ma_window\` (int, default 21): MA window of the internal raw DStat.
- \`vol_window\` (int, default 63): realised-vol window of the internal raw DStat.
- \`pct_window\` (int, default 1260): trailing window (~5 trading years) of raw DStat values the percentile is taken over.
- \`percentile\` (float, default 95.0): percentile to emit, 0..100 scale.

**Edge cases**
- Output is \`NaN\` until a **full** window of \`pct_window\` raw DStat values exists — i.e. for the first \`max(ma_window, 2*vol_window) + pct_window - 1\` bars.
- Any bar whose trailing window contains fewer than \`pct_window\` finite raw values (e.g. a \`vol = 0\` gap left a hole) stays \`NaN\` until the window fills cleanly.
- Nearest-rank index is clamped to \`[0, pct_window-1]\`, so \`percentile = 0\` maps to the window minimum and \`percentile = 100\` to the maximum.
- \`percentile\` outside \`[0, 100]\` → all \`NaN\`.`,
  ownPanel: true,
};
