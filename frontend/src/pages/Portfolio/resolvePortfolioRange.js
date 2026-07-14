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
 * Extract a portfolio (composed) leg's referenced child-portfolio id, or null.
 * SINGLE source of the parity-critical predicate (``portfolioId ||
 * portfolio_id``) — shared by `resolveLegRange`, `childPortfolioIds`, AND the
 * compute-body builder (`computeBodyBuilder.js`) so it can never drift between
 * the active editor, the compute path, and the cache-status probe (a drift
 * would build divergent child sub-bodies → different cache keys).
 *
 * @param {object} leg
 * @returns {string|null}
 */
export function getChildPortfolioId(leg) {
  return (leg && (leg.portfolioId || leg.portfolio_id)) || null;
}

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
    const portfolioId = getChildPortfolioId(leg);
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
 * FUND-OF-FUNDS: resolve the OWN date range of each referenced child portfolio →
 * `Map<portfolioId, {start, end}>`. A child's own range is the OVERLAP of its
 * legs — exactly the `start`/`end` a STANDALONE compute of that child would send
 * (its resolved `overlapRange`). Inlining this into a composed leg's
 * `portfolio.start/end` yields a byte-identical child body → shared backend cache
 * entry (the key-parity invariant). Never throws: an unresolvable child is simply
 * absent from the map (the backend then computes it over its full data overlap).
 *
 * @param {string[]} childIds
 * @param {{ queryClient: object }} deps
 * @returns {Promise<Map<string,{start:string,end:string}>>}
 */
export async function resolveChildRanges(childIds, { queryClient }) {
  const map = new Map();
  const ids = [...new Set((childIds || []).filter(Boolean))];
  await Promise.all(ids.map(async (id) => {
    try {
      const doc = await queryClient.fetchQuery({
        queryKey: queryKeys.persistence.portfolios.detail(id),
        queryFn: () => getPortfolio(id),
        staleTime: 10 * 1000,
      });
      const childLegs = persistedDocToLegs(doc);
      if (childLegs.length === 0) return;
      const { overlapRange } = await resolvePortfolioRange(childLegs, { queryClient });
      if (overlapRange && overlapRange.start && overlapRange.end) {
        map.set(id, { start: overlapRange.start, end: overlapRange.end });
      }
    } catch { /* unresolvable child → absent → backend computes full overlap */ }
  }));
  return map;
}

/**
 * Extract the distinct set of referenced child-portfolio ids from a leg list.
 * SINGLE source of the parity-critical child-id predicate
 * (``portfolioId || portfolio_id``) shared by the active editor, the compute
 * path, and the cache-status probe — so the predicate can never drift between
 * them (a drift would build divergent child sub-bodies → different cache keys).
 *
 * @param {Array} legs
 * @returns {string[]}
 */
export function childPortfolioIds(legs) {
  return [...new Set(
    (legs || [])
      .filter((l) => l.type === 'portfolio' && getChildPortfolioId(l))
      .map((l) => getChildPortfolioId(l)),
  )];
}

/**
 * Fund-of-funds child-range ACCESSOR for a leg list. Collapses the (previously
 * triplicated) ``filter ids → resolveChildRanges → (id)=>map.get(id)||null``
 * ritual into ONE source so the active editor, Compute, and the status probe
 * build byte-identical child sub-bodies (cache-key parity). Returns a sync
 * ``(id) => {start,end}|null`` closure; no child legs ⇒ an accessor that always
 * returns null (empty map, no reads).
 *
 * @param {Array} legs
 * @param {{ queryClient: object }} deps
 * @returns {Promise<(id: string) => ({start:string,end:string}|null)>}
 */
export async function childRangeAccessorFor(legs, { queryClient }) {
  const ids = childPortfolioIds(legs);
  const map = ids.length > 0
    ? await resolveChildRanges(ids, { queryClient })
    : new Map();
  return (id) => map.get(id) || null;
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
