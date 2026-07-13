// Proactive "is this cached?" detection, BACKEND-driven. Builds the compute
// body for the ACTIVE editor config AND each visible saved-list row (the SAME
// buildPortfolioComputeBody the compute path uses), then asks the backend in ONE
// batched call whether each body is already in its result cache. Purely READ:
// never computes, never stores; the backend stays authoritative.
//
// Because the probe body matches what Compute would send (incl. current child
// resolution + resolved range), any edit changes the body → the backend reports
// "not cached" → the indicator flips. That is the visible invalidation signal.

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { hydrateAvailableIndicators } from '../Signals/hydrateIndicators';
import { resolvePortfolioRange } from './resolvePortfolioRange';
import { persistedDocToLegs } from './persistedDoc';
import { buildPortfolioComputeBody } from './computeBodyBuilder';
import { getPortfolioCacheStatus } from '../../api/portfolio';
import { getPortfolio } from '../../api/persistence';
import { queryKeys } from '../../queryKeys';

const CONCURRENCY = 4;
const DEBOUNCE_MS = 300;
const ACTIVE_TAG = '__active__';

/** PURE: map a boolean (or missing) cached flag to a row status string. */
export function statusForCached(cached) {
  return cached ? 'cached' : 'not-cached';
}

// Signature of the data-affecting fields of a persisted doc — changes whenever
// anything that alters the compute body (legs incl. weight/label, rebalance)
// changes, so a memoized row body is invalidated exactly then.
function docSignature(doc) {
  try {
    return JSON.stringify({ legs: doc.legs || [], rebalance: doc.rebalance || 'none' });
  } catch {
    return String(doc && doc.id);
  }
}

// Bounded async worker pool.
async function runPool(items, limit, worker) {
  let idx = 0;
  const n = Math.min(limit, items.length);
  const runners = Array.from({ length: n }, async () => {
    while (idx < items.length) {
      const i = idx;
      idx += 1;
      // eslint-disable-next-line no-await-in-loop
      await worker(items[i]);
    }
  });
  await Promise.all(runners);
}

/**
 * @param {Object} p
 * @param {boolean} p.cacheEnabled            gate — off ⇒ no probe, empty result
 * @param {Array}   p.legs                    active editor legs
 * @param {string}  p.rebalance               active rebalance
 * @param {string}  p.startDate               active explicit start ('' = none)
 * @param {string}  p.endDate                 active explicit end ('' = none)
 * @param {{start,end}|null} p.overlapRange   active resolved range (fallback)
 * @param {(id:string)=>object|null} p.resolvePortfolio  child resolver (active + rows)
 * @param {Array}   p.portfolios              visible saved rows [{id, legs, rebalance}]
 * @param {string|null} p.activeId            currently-loaded row id (status from active)
 * @param {number}  p.refreshKey              bump to re-probe (e.g. after a compute)
 * @returns {{ activeCached: boolean|null, rowStatusById: Record<string,'checking'|'cached'|'not-cached'> }}
 */
export default function usePortfolioCacheStatus({
  cacheEnabled,
  legs,
  rebalance,
  startDate,
  endDate,
  overlapRange,
  resolvePortfolio,
  portfolios,
  activeId,
  refreshKey = 0,
}) {
  const queryClient = useQueryClient();
  const [activeCached, setActiveCached] = useState(null);
  const [rowStatusById, setRowStatusById] = useState({});
  // Per-row memo { sig, body } so a re-probe reuses the resolved body (no
  // repeated dwh range reads) until the row's data-affecting fields change.
  const bodyCacheRef = useRef(new Map());
  const runIdRef = useRef(0);

  useEffect(() => {
    if (!cacheEnabled) {
      setActiveCached(null);
      setRowStatusById({});
      return undefined;
    }

    const runId = runIdRef.current + 1;
    runIdRef.current = runId;
    let cancelled = false;
    const live = () => !cancelled && runId === runIdRef.current;

    const rows = Array.isArray(portfolios) ? portfolios : [];
    // Seed unseen rows as `checking` (no layout jump; keep known states).
    setRowStatusById((prev) => {
      const next = { ...prev };
      for (const p of rows) if (!(p.id in next)) next[p.id] = 'checking';
      return next;
    });

    const timer = setTimeout(async () => {
      let availableIndicators = [];
      try {
        availableIndicators = await hydrateAvailableIndicators();
      } catch {
        availableIndicators = [];
      }
      if (!live()) return;

      // ── Build the ACTIVE body (if keyable) ──
      const queries = [];       // { tag, body }
      const effStart = startDate || overlapRange?.start;
      const effEnd = endDate || overlapRange?.end;
      if (legs.length > 0 && effStart && effEnd) {
        try {
          const { body, missing, brokenRefs = [] } = buildPortfolioComputeBody({
            legs, rebalance, start: effStart, end: effEnd, availableIndicators, resolvePortfolio,
          });
          if (!missing.length && !brokenRefs.length) queries.push({ tag: ACTIVE_TAG, body });
        } catch { /* un-keyable active config → no active query (stays null) */ }
      }

      // Resolve a saved ROW's OWN referenced child portfolios (by id) → a sync
      // ``(id) => doc|null`` resolver over the fetched current specs. A row's
      // children are NOT in the active editor's ``resolvePortfolio`` map (that
      // only knows the loaded config's children), so composed rows MUST resolve
      // their own — otherwise every non-active composed row inlines nothing →
      // brokenRef → omitted → falsely "not cached" (FE-B1). Mirrors the child
      // resolution in resolvePortfolioRange.js's portfolio branch; fetches go
      // through React Query (deduped/cached; child edits invalidate the detail).
      const resolveRowChildren = async (rowLegs) => {
        const ids = [...new Set(
          rowLegs
            .filter((l) => l.type === 'portfolio' && (l.portfolioId || l.portfolio_id))
            .map((l) => l.portfolioId || l.portfolio_id),
        )];
        if (ids.length === 0) return () => null;
        const pairs = await Promise.all(ids.map(async (id) => {
          try {
            const doc = await queryClient.fetchQuery({
              queryKey: queryKeys.persistence.portfolios.detail(id),
              queryFn: () => getPortfolio(id),
              staleTime: 10 * 1000,
            });
            return [id, doc];
          } catch {
            return [id, null];
          }
        }));
        const map = Object.fromEntries(pairs);
        return (id) => {
          const doc = map[id];
          if (!doc) return null;
          if (doc.category === 'ARCHIVE' || doc.category === 'DELETED') return null;
          if (!Array.isArray(doc.legs) || doc.legs.length === 0) return null;
          return doc;
        };
      };

      // ── Build ROW bodies (the active row is derived from activeCached) ──
      await runPool(rows, CONCURRENCY, async (doc) => {
        if (!live() || doc.id === activeId) return;
        try {
          const rowLegs = persistedDocToLegs(doc);
          const hasChildRefs = rowLegs.some((l) => l.type === 'portfolio');
          const sig = docSignature(doc);
          const memo = bodyCacheRef.current.get(doc.id);
          // Memoize PURE rows only. A composed row's inlined child spec can
          // change without the row's OWN legs changing (docSignature unchanged),
          // so it must rebuild each probe to stay content-addressed on the
          // current child (the child fetch is still React-Query-cached).
          let body = (!hasChildRefs && memo && memo.sig === sig) ? memo.body : null;
          if (!body) {
            const { overlapRange: ov } = await resolvePortfolioRange(rowLegs, { queryClient });
            if (ov && ov.start && ov.end) {
              // Resolve THIS row's own children (composed rows) — not the active
              // editor's resolver — so its status body inlines its real specs.
              const rowResolver = hasChildRefs ? await resolveRowChildren(rowLegs) : () => null;
              if (!live()) return;
              const built = buildPortfolioComputeBody({
                legs: rowLegs,
                rebalance: doc.rebalance || 'none',
                start: ov.start,
                end: ov.end,
                availableIndicators,
                resolvePortfolio: rowResolver,
              });
              if (!built.missing.length && !(built.brokenRefs && built.brokenRefs.length)) {
                body = built.body;
                if (!hasChildRefs) bodyCacheRef.current.set(doc.id, { sig, body });
              }
            }
          }
          if (body) queries.push({ tag: doc.id, body });
        } catch { /* dwh flake / un-keyable row → omitted → not-cached below */ }
      });
      if (!live()) return;

      // ── ONE batched status call for every keyable body ──
      let results = [];
      if (queries.length > 0) {
        try {
          const res = await getPortfolioCacheStatus(queries.map((q) => q.body));
          results = Array.isArray(res?.results) ? res.results : [];
        } catch {
          results = []; // endpoint error → treat everything as not-cached
        }
      }
      if (!live()) return;

      // ── Map results back by position ──
      const cachedByTag = {};
      queries.forEach((q, i) => { cachedByTag[q.tag] = !!(results[i] && results[i].cached); });

      setActiveCached(ACTIVE_TAG in cachedByTag ? cachedByTag[ACTIVE_TAG] : null);
      setRowStatusById((prev) => {
        const next = { ...prev };
        for (const doc of rows) {
          if (doc.id === activeId) continue; // active row derives from activeCached
          next[doc.id] = statusForCached(cachedByTag[doc.id]);
        }
        return next;
      });
    }, DEBOUNCE_MS);

    return () => { cancelled = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    cacheEnabled, legs, rebalance, startDate, endDate, overlapRange,
    resolvePortfolio, portfolios, activeId, refreshKey, queryClient,
  ]);

  return cacheEnabled
    ? { activeCached, rowStatusById }
    : { activeCached: null, rowStatusById: {} };
}
