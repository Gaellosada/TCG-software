// User-visible banner copy — strings preserved verbatim.
export function computeDefaultSeriesBannerText({
  defaultSeriesLoaded,
  defaultSeries,
  defaultSeriesError,
}) {
  if (!defaultSeriesLoaded) return null;
  if (defaultSeries) return null;
  if (defaultSeriesError) {
    const k = defaultSeriesError.kind;
    if (k === 'offline') return "You're offline — series list unavailable";
    if (k === 'network') return "Can't reach the data server";
    if (k === 'server' || k === 'client') {
      return `Data server error: ${defaultSeriesError.message || 'unknown'}`;
    }
    // 'not-found' / 'unknown' → fall through to classic copy.
  }
  return 'S&P 500 not found in DB — pick a series manually.';
}
