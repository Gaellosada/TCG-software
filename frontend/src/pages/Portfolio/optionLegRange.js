// Resolve the available date range for an option_stream leg.
//
// Unlike a priced instrument/continuous leg (whose series carries explicit
// dates), an option stream has no single price series — its selectable window
// is the option bar coverage (first..last trade_date), SCOPED to the leg's own
// expiration cycle when it has one. We read that from GET /api/options/coverage
// (passing the cycle) so an option leg contributes a REAL range to the portfolio
// overlap, exactly like every other leg. This removes the old artificial
// ``today-5y`` default that floored option-only portfolios at ~2021.
//
// Cycle scoping matters: a per-cycle series (e.g. 'W3 Friday'/EW3, whose data
// starts ~2016) begins years after the collection as a whole (~2011). Querying
// coverage WITHOUT the cycle would advertise the earlier collection floor, and
// the resolved portfolio range would start before the leg has any data → a
// misleading flat pre-data segment. Passing the cycle floors the range at the
// cycle's real data start. A cycle-less leg keeps the whole-collection span
// (byte-identical call).
//
// Nuance: coverage is the PRICE span. A by_delta selection may only resolve
// contracts from when stored/computed deltas begin (e.g. 2007 for SPX vs prices
// from 2005). That narrowing happens at compute time and is acceptable — the
// point here is to expose the true history, not an artificial recent floor. See
// the coverage endpoint docstring.
import { getOptionCoverage } from '../../api/options';
import { queryKeys } from '../../queryKeys';

export async function fetchOptionLegRange(queryClient, leg, dataSource = 'v1') {
  if (!leg.collection) {
    return { id: leg.id, start: null, end: null, recommendedStart: null, segments: [] };
  }
  try {
    // Scope the coverage span to the leg's OWN expiration cycle. A cycle like
    // 'W3 Friday' (EW3) has data starting years after the collection floor, so
    // querying whole-collection coverage would advertise a range (~2011) that
    // starts before the leg has any data → a misleading flat pre-data segment.
    // ''/undefined/null collapse to null → the coverage call stays byte-identical
    // to the pre-cycle path (only 2 args on the wire helper, no expiration_cycle).
    const cycle = leg.cycle || null;
    const coverageArgs = cycle
      ? [leg.collection, dataSource, cycle]
      : [leg.collection, dataSource];
    const res = await queryClient.fetchQuery({
      queryKey: queryKeys.market.optionCoverage(leg.collection, dataSource, cycle),
      queryFn: () => getOptionCoverage(...coverageArgs),
    });
    const start = res?.start || null;
    const end = res?.end || null;
    if (start && end && start <= end) {
      // Additive cadence fields (backend §A.2). ``recommended_start`` is the
      // full-cadence floor (== start when there is no cliff); ``segments`` are
      // the contiguous cadence spans used to seed the default window, drive the
      // overlap warning, and shade the lower-cadence band. Absent (older backend
      // / non-segmented) → default to the raw start / empty band.
      return {
        id: leg.id,
        start,
        end,
        recommendedStart: res?.recommended_start ?? start,
        segments: res?.segments ?? [],
      };
    }
    return { id: leg.id, start: null, end: null, recommendedStart: null, segments: [] };
  } catch {
    return { id: leg.id, start: null, end: null, recommendedStart: null, segments: [] };
  }
}
