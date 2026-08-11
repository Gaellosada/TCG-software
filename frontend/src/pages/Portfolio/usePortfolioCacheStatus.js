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
import { resolvePortfolioRange, childRangeAccessorFor } from './resolvePortfolioRange';
import { persistedDocToLegs } from './persistedDoc';
import { buildPortfolioComputeBody } from './computeBodyBuilder';
import { getPortfolioCacheStatus } from '../../api/portfolio';
import { getPortfolio } from '../../api/persistence';
import { getSlippageBps, getFeesBps } from '../../lib/userSettings';
import { queryKeys } from '../../queryKeys';

const CONCURRENCY = 4;
const DEBOUNCE_MS = 300;

/** PURE: map a boolean (or missing) cached flag to a row status string. */
export function statusForCached(cached) {
  return cached ? 'cached' : 'not-cached';
}

// Signature of the data-affecting fields of a persisted doc — changes whenever
// anything that alters the compute body (legs incl. weight/label, rebalance,
// and each leg's own per-instrument data source) changes, so a memoized row
// body is invalidated exactly then. Per-leg ``dataSource`` rides inside
// ``doc.legs`` so it is captured here automatically.
function docSignature(doc) {
  try {
    return JSON.stringify({
      legs: doc.legs || [],
      rebalance: doc.rebalance || 'none',
    });
  } catch {
    return `${doc && doc.id}`;
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

      // Global execution costs — read once per probe run and threaded into EVERY
      // built body (active + rows) so a probe body matches what Compute sends;
      // otherwise the backend cache key would differ when costs are non-zero and
      // the "cached" indicator would be wrong.
      const slippageBps = getSlippageBps();
      const feesBps = getFeesBps();

      // Cheap per-body cache-status peek → boolean `cached`. The backend status
      // call is a SQLite key peek, so firing one small call per row (instead of
      // a single batched call gated behind the SLOWEST row) is cheap and lets
      // each label commit the instant its OWN body resolves. An endpoint error
      // is treated as not-cached (same as the old batched-failure fallback).
      const probeCached = async (body) => {
        try {
          const res = await getPortfolioCacheStatus([body]);
          const results = Array.isArray(res?.results) ? res.results : [];
          return !!(results[0] && results[0].cached);
        } catch {
          return false;
        }
      };

      // Commit exactly ONE row's status. Stale-guarded: a superseded run's
      // late-arriving result must never overwrite a newer run's state, so bail
      // unless THIS run is still current (per-run token via `live()`).
      const commitRow = (id, cached) => {
        if (!live()) return;
        setRowStatusById((prev) => ({ ...prev, [id]: statusForCached(cached) }));
      };

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

      // ── ACTIVE probe — independent of the rows; commits as soon as it keys ──
      // Mirror the active editor's window default (usePortfolio): probe the SAME
      // key Compute will use, i.e. seed from the cadence recommendation, not the
      // raw overlap start. Runs concurrently with the row pool so neither blocks
      // the other.
      const activeTask = (async () => {
        const effStart = startDate || overlapRange?.recommendedStart || overlapRange?.start;
        const effEnd = endDate || overlapRange?.end;
        let activeBody = null;
        if (legs.length > 0 && effStart && effEnd) {
          try {
            // Fund-of-funds key parity: resolve each active child's OWN range so
            // the composed body matches what Compute/auto-display send (shared
            // single-source accessor keeps the id predicate + wiring identical).
            const resolveChildRange = await childRangeAccessorFor(legs, { queryClient });
            if (!live()) return;
            const { body, missing, brokenRefs = [] } = buildPortfolioComputeBody({
              legs, rebalance, start: effStart, end: effEnd, availableIndicators, resolvePortfolio,
              resolveChildRange, slippageBps, feesBps,
            });
            if (!missing.length && !brokenRefs.length) activeBody = body;
          } catch { /* un-keyable active config → active stays null */ }
        }
        if (!live()) return;
        if (activeBody) {
          const cached = await probeCached(activeBody);
          if (!live()) return;
          setActiveCached(cached);
        } else {
          setActiveCached(null);
        }
      })();

      // ── ROW probes — each row commits the INSTANT its own range resolves ──
      // A bounded pool resolves each row's body; the moment a body is ready its
      // status is probed and committed, so fast rows show while slow rows stay
      // `checking` (no all-or-nothing barrier). The active row is skipped here —
      // its label derives from `activeCached`.
      const rowsTask = runPool(rows, CONCURRENCY, async (doc) => {
        if (!live() || doc.id === activeId) return;
        let body = null;
        try {
          const rowLegs = persistedDocToLegs(doc);
          const hasChildRefs = rowLegs.some((l) => l.type === 'portfolio');
          const sig = docSignature(doc);
          const memo = bodyCacheRef.current.get(doc.id);
          // Memoize PURE rows only. A composed row's inlined child spec can
          // change without the row's OWN legs changing (docSignature unchanged),
          // so it must rebuild each probe to stay content-addressed on the
          // current child (the child fetch is still React-Query-cached).
          body = (!hasChildRefs && memo && memo.sig === sig) ? memo.body : null;
          if (!body) {
            const { overlapRange: ov } = await resolvePortfolioRange(rowLegs, { queryClient });
            if (ov && ov.start && ov.end) {
              // Resolve THIS row's own children (composed rows) — not the active
              // editor's resolver — so its status body inlines its real specs
              // AND (fund-of-funds) each child's own range, for key parity.
              const rowResolver = hasChildRefs ? await resolveRowChildren(rowLegs) : () => null;
              const resolveChildRange = await childRangeAccessorFor(rowLegs, { queryClient });
              if (!live()) return;
              const built = buildPortfolioComputeBody({
                legs: rowLegs,
                rebalance: doc.rebalance || 'none',
                // Mirror the ACTIVE probe and the compute sites: seed from the
                // cadence recommendation so a cadence-cliff option row's status
                // body keys the SAME entry Compute wrote (else false
                // "not-cached"). Falls back to raw start when there's no cliff.
                start: ov.recommendedStart || ov.start,
                end: ov.end,
                availableIndicators,
                resolvePortfolio: rowResolver,
                resolveChildRange,
                slippageBps,
                feesBps,
              });
              if (!built.missing.length && !(built.brokenRefs && built.brokenRefs.length)) {
                body = built.body;
                if (!hasChildRefs) bodyCacheRef.current.set(doc.id, { sig, body });
              }
            }
          }
        } catch {
          body = null; // dwh flake / un-keyable row → not-cached below
        }
        if (!live()) return;
        // Probe + commit THIS row alone. A body that never keyed (broken ref /
        // flake) commits `not-cached`, matching the old batched fallback. Both
        // `probeCached` and `commitRow` are stale-guarded, so a superseded run's
        // late result is dropped rather than overwriting the current run.
        if (body) {
          const cached = await probeCached(body);
          commitRow(doc.id, cached);
        } else {
          commitRow(doc.id, false);
        }
      });

      await Promise.all([activeTask, rowsTask]);
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
