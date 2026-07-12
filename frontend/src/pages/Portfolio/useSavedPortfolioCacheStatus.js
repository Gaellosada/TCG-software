// Per-row "is this saved portfolio cached?" detection for the Saved Portfolios
// list. ACCURATE semantics: a row is `cached` iff opening it would show a result
// instantly — i.e. its EXACT current cache key (same body builder + same range
// resolution as the active/compute path) is present in IndexedDB.
//
// Read-only: never auto-loads or computes anything.

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { hydrateAvailableIndicators } from '../Signals/hydrateIndicators';
import { resolvePortfolioRange } from './resolvePortfolioRange';
import { persistedDocToLegs } from './persistedDoc';
import { buildPortfolioComputeBody } from './computeBodyBuilder';
import { computeCacheKey } from '../../lib/computeCacheKey';
import { hasCached } from '../../lib/portfolioCache';

const CONCURRENCY = 4;

/** PURE: map a (key, presence) pair to a row status. */
export function statusForKey(key, present) {
  if (!key) return 'not-cached';
  return present ? 'cached' : 'not-cached';
}

// Signature of the data-affecting fields of a persisted doc. Changes whenever
// anything that alters the compute body (legs incl. weight/label, rebalance)
// changes → invalidates the cached key for that row.
function docSignature(doc) {
  try {
    return JSON.stringify({ legs: doc.legs || [], rebalance: doc.rebalance || 'none' });
  } catch {
    return String(doc && doc.id);
  }
}

// Resolve a bounded number of async workers over `items`.
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
 * @param {Array}  p.portfolios   persisted docs [{id, legs, rebalance, ...}]
 * @param {boolean} p.cacheEnabled
 * @param {number} p.cacheVersion bump → re-check hasCached (cheap; keys reused)
 * @param {string|null} p.activeId  currently-loaded portfolio id — SKIPPED here;
 *                                   its row status is supplied by the page from
 *                                   `badgeCached` (mergedRowCacheStatus), so we
 *                                   never compute or re-scan IDB for it.
 * @returns {Record<string,'checking'|'cached'|'not-cached'>}
 */
export default function useSavedPortfolioCacheStatus({
  portfolios,
  cacheEnabled,
  cacheVersion,
  activeId,
}) {
  const queryClient = useQueryClient();
  const [statusById, setStatusById] = useState({});
  // Per-portfolioId cache of { sig, key } so a re-check (e.g. cacheVersion bump)
  // reuses the resolved key instead of re-hitting dwh for the ranges.
  const keyCacheRef = useRef(new Map());
  const runIdRef = useRef(0);

  useEffect(() => {
    if (!cacheEnabled) {
      setStatusById({});
      return undefined;
    }
    const rows = Array.isArray(portfolios) ? portfolios : [];
    if (rows.length === 0) {
      setStatusById({});
      return undefined;
    }

    const runId = runIdRef.current + 1;
    runIdRef.current = runId;
    let cancelled = false;
    const live = () => !cancelled && runId === runIdRef.current;

    // Seed unseen rows as `checking` (no layout jump; keep known states).
    setStatusById((prev) => {
      const next = { ...prev };
      for (const p of rows) if (!(p.id in next)) next[p.id] = 'checking';
      return next;
    });

    (async () => {
      // Hydrate indicators ONCE, shared across all rows. Degrade to [] on error.
      let availableIndicators = [];
      try {
        availableIndicators = await hydrateAvailableIndicators();
      } catch {
        availableIndicators = [];
      }
      if (!live()) return;

      await runPool(rows, CONCURRENCY, async (doc) => {
        if (!live()) return;
        // The active row is authoritatively supplied by the page from
        // `badgeCached` (mergedRowCacheStatus overrides it), so anything we'd
        // compute here is discarded. Skip it entirely — no key resolution, no
        // IDB scan — so editing the active portfolio doesn't re-scan the whole
        // saved list.
        if (doc.id === activeId) return;
        let key = null;
        try {
          const sig = docSignature(doc);
          const cached = keyCacheRef.current.get(doc.id);
          if (cached && cached.sig === sig) {
            key = cached.key;
          } else {
            const legs = persistedDocToLegs(doc);
            const { overlapRange } = await resolvePortfolioRange(legs, { queryClient });
            if (overlapRange && overlapRange.start && overlapRange.end) {
              const { body, missing } = buildPortfolioComputeBody({
                legs,
                rebalance: doc.rebalance || 'none',
                start: overlapRange.start,
                end: overlapRange.end,
                availableIndicators,
              });
              if (!missing.length) key = await computeCacheKey(body);
            }
            // Only memoize a SUCCESSFULLY-resolved key. A transient dwh flake
            // resolves the range to {start:null,end:null} (never throws) → key
            // stays null; caching {sig, key:null} would permanently mark an
            // actually-cached row "Not cached" for the session. Leaving it
            // unmemoized lets the next cacheVersion bump retry the resolve.
            if (key) keyCacheRef.current.set(doc.id, { sig, key });
          }
        } catch {
          key = null; // dwh flake / hash error → treat row as not-cached
        }
        if (!live()) return;

        let present = false;
        if (key) {
          try {
            present = await hasCached(key);
          } catch {
            present = false;
          }
        }
        if (!live()) return;
        setStatusById((prev) => ({ ...prev, [doc.id]: statusForKey(key, present) }));
      });
    })();

    return () => { cancelled = true; };
  }, [portfolios, cacheEnabled, cacheVersion, activeId, queryClient]);

  return cacheEnabled ? statusById : {};
}
