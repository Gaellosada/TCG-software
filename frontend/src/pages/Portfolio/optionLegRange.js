// Resolve the available date range for an option_stream leg.
//
// Unlike a priced instrument/continuous leg (whose series carries explicit
// dates), an option stream has no single price series — its selectable window
// is the option COLLECTION's bar coverage (first..last trade_date). We read
// that from GET /api/options/coverage so an option leg contributes a REAL
// range to the portfolio overlap, exactly like every other leg. This removes
// the old artificial ``today-5y`` default that floored option-only portfolios
// at ~2021.
//
// Nuance: coverage is the collection's PRICE span. A by_delta selection may
// only resolve contracts from when stored/computed deltas begin (e.g. 2007 for
// SPX vs prices from 2005). That narrowing happens at compute time and is
// acceptable — the point here is to expose the true multi-decade history, not
// an artificial recent floor. See the coverage endpoint docstring.
import { getOptionCoverage } from '../../api/options';
import { queryKeys } from '../../queryKeys';

export async function fetchOptionLegRange(queryClient, leg) {
  if (!leg.collection) {
    return { id: leg.id, start: null, end: null };
  }
  try {
    const res = await queryClient.fetchQuery({
      queryKey: queryKeys.market.optionCoverage(leg.collection),
      queryFn: () => getOptionCoverage(leg.collection),
    });
    const start = res?.start || null;
    const end = res?.end || null;
    if (start && end && start <= end) {
      return { id: leg.id, start, end };
    }
    return { id: leg.id, start: null, end: null };
  } catch {
    return { id: leg.id, start: null, end: null };
  }
}
