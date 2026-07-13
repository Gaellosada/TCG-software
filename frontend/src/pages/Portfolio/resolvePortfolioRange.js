// Shared portfolio date-range resolution.
//
// The active editor effect (usePortfolio) AND the saved-list cache-status
// detection BOTH must resolve a portfolio's `start`/`end` the exact same way,
// or the row "cached" icons would use a different key than the active/compute
// path and lie. This is the single source of that logic (extracted verbatim
// from the usePortfolio effect).

import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import { getPortfolio } from '../../api/persistence';
import { queryKeys } from '../../queryKeys';
import { formatDateInt } from '../../utils/format';
import { fetchSignalLegRange } from './signalLegRange';
import { fetchOptionLegRange } from './optionLegRange';
import { persistedDocToLegs } from './persistedDoc';

/**
 * Resolve one leg's available range → `{ id, start, end }` (ISO strings or
 * null). Never throws — a failed/empty read degrades to nulls.
 *
 * ``_depth`` guards the composed-portfolio recursion (depth-1): a portfolio leg
 * nested inside a child is not resolved (returns nulls), mirroring the compute
 * builder + backend guard.
 */
export async function resolveLegRange(leg, { queryClient }, _depth = 0) {
  if (leg.type === 'portfolio') {
    // Composed leg: its available range is the OVERLAP of its referenced child's
    // legs (the same grid the backend/compute builder use). Without this, a
    // composed portfolio resolves NO range → overlapRange null → the compute
    // window (and slider) can't settle on the composed page.
    const portfolioId = leg.portfolioId || leg.portfolio_id || null;
    if (!portfolioId || _depth >= 1) return { id: leg.id, start: null, end: null };
    try {
      const child = await queryClient.fetchQuery({
        queryKey: queryKeys.persistence.portfolios.detail(portfolioId),
        queryFn: () => getPortfolio(portfolioId),
        staleTime: 10 * 1000,
      });
      const childLegs = persistedDocToLegs(child);
      if (childLegs.length === 0) return { id: leg.id, start: null, end: null };
      const childResults = await Promise.all(
        childLegs.map((cl) => resolveLegRange(cl, { queryClient }, _depth + 1)),
      );
      const overlap = overlapRangeOf(childResults);
      return { id: leg.id, start: overlap?.start ?? null, end: overlap?.end ?? null };
    } catch {
      return { id: leg.id, start: null, end: null };
    }
  }
  if (leg.type === 'signal') {
    return fetchSignalLegRange(leg);
  }
  if (leg.type === 'option_stream') {
    return fetchOptionLegRange(queryClient, leg);
  }
  try {
    let dates;
    if (leg.type === 'continuous') {
      const params = {
        strategy: leg.strategy || 'front_month',
        adjustment: leg.adjustment || 'none',
        cycle: leg.cycle || undefined,
        rollOffset: leg.rollOffset || 0,
        rank: leg.rank || 1,
      };
      const res = await queryClient.fetchQuery({
        queryKey: queryKeys.market.continuous(leg.collection, params),
        queryFn: () => getContinuousSeries(leg.collection, params),
      });
      dates = res?.dates;
    } else {
      const res = await queryClient.fetchQuery({
        queryKey: queryKeys.market.prices(leg.collection, leg.symbol),
        queryFn: () => getInstrumentPrices(leg.collection, leg.symbol),
      });
      dates = res?.dates;
    }
    if (dates && dates.length > 0) {
      return {
        id: leg.id,
        start: formatDateInt(dates[0]),
        end: formatDateInt(dates[dates.length - 1]),
      };
    }
    return { id: leg.id, start: null, end: null };
  } catch {
    return { id: leg.id, start: null, end: null };
  }
}

/**
 * PURE: overlap of per-leg ranges = latest start → earliest end. Returns
 * `{ start, end }` or null (no valid leg, or the ranges don't overlap).
 */
export function overlapRangeOf(perLegResults) {
  const starts = [];
  const ends = [];
  for (const r of perLegResults) {
    if (r && r.start) {
      starts.push(r.start);
      ends.push(r.end);
    }
  }
  if (starts.length === 0) return null;
  const overlapStart = starts.reduce((a, b) => (a > b ? a : b));
  const overlapEnd = ends.reduce((a, b) => (a < b ? a : b));
  return overlapStart <= overlapEnd ? { start: overlapStart, end: overlapEnd } : null;
}

/**
 * Resolve every leg's range and the portfolio overlap.
 * @returns {Promise<{ ranges: Record<string,{start,end}>, overlapRange: {start,end}|null }>}
 * Never throws (each leg read is wrapped).
 */
export async function resolvePortfolioRange(legs, { queryClient }) {
  if (!legs || legs.length === 0) return { ranges: {}, overlapRange: null };
  const results = await Promise.all(legs.map((leg) => resolveLegRange(leg, { queryClient })));
  const ranges = {};
  for (const r of results) ranges[r.id] = { start: r.start, end: r.end };
  return { ranges, overlapRange: overlapRangeOf(results) };
}
