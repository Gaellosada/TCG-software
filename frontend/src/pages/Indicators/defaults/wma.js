// Weighted Moving Average — linearly increasing weights 1..window.
// Implemented via convolution with the (reversed) weight kernel so the most
// recent bar gets weight ``window`` and the oldest gets weight 1.
const code = `def compute(series, window: int = 20):
    s = series['close']
    out = np.full_like(s, np.nan, dtype=float)
    weights = np.arange(1, window + 1, dtype=float)
    weights = weights / np.sum(weights)
    # np.convolve flips the kernel; reverse the weights so index 0 of the
    # output window aligns with the oldest bar and weight 1.
    kernel = weights[::-1]
    out[window-1:] = np.convolve(s, kernel, mode='valid')
    return out`;

export default {
  id: 'wma',
  name: 'WMA',
  readonly: true,
  code,
  params: {},
  seriesMap: {},
  doc: `Weighted Moving Average — linearly increasing weights over closing prices so the most recent bar has weight \`window\` and the oldest has weight 1. Output is NaN for the first \`window - 1\` bars.

**Parameters**
- \`window\`: number of bars in the weighting window. Smaller values track price more closely; larger values smooth more.`,
};
