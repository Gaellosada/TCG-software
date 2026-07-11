import { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { computePortfolio } from '../../api/portfolio';
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import { queryKeys } from '../../queryKeys';
import { formatDateInt } from '../../utils/format';
import { hydrateAvailableIndicators } from '../Signals/hydrateIndicators';
import { fetchSignalLegRange } from './signalLegRange';
import { fetchOptionLegRange } from './optionLegRange';
import { legsToRangesKey } from './legKey';
import { buildPortfolioComputeBody } from './computeBodyBuilder';
import { shouldDisplayComputeResult } from './cacheDisplayPolicy';
import { computeCacheKey } from '../../lib/computeCacheKey';
import { getCached, putCached } from '../../lib/portfolioCache';
import { isPortfolioCacheEnabled } from '../../lib/userSettings';
import useAbortableAction from '../../hooks/useAbortableAction';

let nextId = 1;

// Pick a label that doesn't collide with any existing leg — API dict keys would collapse on duplicates.
function uniqueLegLabel(desired, existingLegs) {
  const existing = new Set(existingLegs.map((l) => l.label));
  if (!existing.has(desired)) return desired;
  let n = 2;
  while (existing.has(`${desired} (${n})`)) n++;
  return `${desired} (${n})`;
}

/**
 * Custom hook managing all portfolio state: legs, config, API calls, save/load.
 *
 * The API expects legs as a dict: { label: { type, collection, symbol, ... } }
 * and weights as a dict: { label: weight }.
 */
export default function usePortfolio() {
  // Leg-range reads share the app-wide market-data cache via fetchQuery —
  // a leg whose price/continuous series was already loaded on the Data page
  // resolves from cache (no refetch), and otherwise populates the same cache
  // entry the Data page will later read. Same query keys as the Data hooks.
  const queryClient = useQueryClient();
  const [legs, setLegs] = useState([]);
  const [rebalance, setRebalance] = useState('none');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [results, setResults] = useState(null);
  const { run: runAbortable, running: loading, abort: abortCalculate } = useAbortableAction();
  const [error, setError] = useState(null);
  const [legDateRanges, setLegDateRanges] = useState({});
  const [overlapRange, setOverlapRange] = useState(null);
  const [rangesLoading, setRangesLoading] = useState(false);
  const [portfolioName, setPortfolioName] = useState('');
  const [dirty, setDirty] = useState(false);
  // Autosave toggle — now controls backend autosave (3s debounce via
  // useBackendAutosave at the page level). The old localStorage-based
  // initialisation is kept commented for reference.
  // Old: () => localStorage.getItem(AUTOSAVE_KEY) === 'true'
  const [autosave, setAutosaveState] = useState(true);
  // ID of the backend-persisted portfolio doc currently loaded into the
  // editor. When non-null, every editable change is debounce-PUT to
  // /api/persistence/portfolios/{persistedId}. Every portfolio now has a
  // persistedId from the moment it is created (always in backend).
  const [persistedId, setPersistedId] = useState(null);
  // The category of the currently loaded portfolio. Tracked here so the
  // backend autosave payload can include it without depending on the
  // page-level persistedPortfolios list.
  const [persistedCategory, setPersistedCategory] = useState('RESEARCH');
  // Whether the currently loaded portfolio is locked (read-only).
  // Mirrors the `locked` field from the backend doc; updated on load and
  // when the lock API call returns an updated doc.
  const [persistedLocked, setPersistedLocked] = useState(false);

  // ── Local portfolio-result cache (opt-in; Settings toggle, default OFF) ──
  // Read once at mount (mirrors the userSettings convention: a toggle change
  // applies on the next mount). When off, every cache branch below is skipped
  // and behavior is byte-for-byte today's.
  const [cacheEnabled] = useState(() => isPortfolioCacheEnabled());
  // The cache key for the CURRENT editor state, recomputed reactively for the
  // badge. Null while gated (no legs / ranges unresolved / un-keyable body).
  const [currentCacheKey, setCurrentCacheKey] = useState(null);
  // Bumped after every compute completes so the auto-display/badge effect
  // re-syncs the displayed result and the badge to the freshly-cached state.
  const [cacheVersion, setCacheVersion] = useState(0);
  // RACE GUARDS for the auto-display effect (cache-ON only):
  //  - computingRef: true while a compute is in flight → the effect must not
  //    touch `results` (the compute owns it).
  //  - computeSeqRef: incremented at the START of every compute; the effect
  //    captures it and refuses to setResults if it changed mid-read (a compute
  //    started/ran during the async hash+getCached), so a stale cache read can
  //    never clobber a fresh compute result. Deterministic — does not rely on
  //    React effect-cleanup timing.
  const computingRef = useRef(false);
  const computeSeqRef = useRef(0);
  // Live cache key mirror (FIX A): always holds the key of the CURRENT config,
  // updated synchronously by the auto-display effect. A landing compute compares
  // the key it ran for against this; if the user edited mid-flight the keys
  // differ and the stale result is dropped (not shown for the modified config).
  const currentKeyRef = useRef(null);

  /* ── Fetch date ranges when legs change ── */

  // Stable key: only data-affecting fields (not label/weight) trigger re-fetch
  const rangesKey = useMemo(() => legsToRangesKey(legs), [legs]);

  useEffect(() => {
    if (legs.length === 0) {
      setLegDateRanges({});
      setOverlapRange(null);
      return;
    }

    let cancelled = false;
    setRangesLoading(true);

    // Fetch each leg's price data using the same APIs as the Data page.
    // Signal legs derive their range from the overlap of their inputs' ranges.
    const promises = legs.map(async (leg) => {
      if (leg.type === 'signal') {
        return fetchSignalLegRange(leg);
      }
      if (leg.type === 'option_stream') {
        // An option stream's range is the option COLLECTION's bar coverage
        // (first..last trade_date), fetched from /api/options/coverage. This
        // makes an option leg contribute a REAL range to the overlap — exactly
        // like every other leg — instead of the old null that forced an
        // artificial today-5y (~2021) floor.
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
    });

    Promise.all(promises).then((results) => {
      if (cancelled) return;

      const ranges = {};
      const validStarts = [];
      const validEnds = [];

      for (const r of results) {
        ranges[r.id] = { start: r.start, end: r.end };
        if (r.start) {
          validStarts.push(r.start);
          validEnds.push(r.end);
        }
      }

      setLegDateRanges(ranges);

      if (validStarts.length > 0) {
        // Overlap = latest start to earliest end
        const overlapStart = validStarts.reduce((a, b) => (a > b ? a : b));
        const overlapEnd = validEnds.reduce((a, b) => (a < b ? a : b));
        if (overlapStart <= overlapEnd) {
          setOverlapRange({ start: overlapStart, end: overlapEnd });
        } else {
          setOverlapRange(null);
        }
      } else {
        // No leg resolved a range (e.g. ranges not yet settled or all reads
        // failed). Option legs now resolve their real coverage above and flow
        // through the same overlap logic as every other leg — there is no
        // longer a special-case today-5y default that floored option-only
        // portfolios at ~2021.
        setOverlapRange(null);
      }

      setRangesLoading(false);
    }).catch(() => {
      if (!cancelled) setRangesLoading(false);
    });

    return () => { cancelled = true; };
  }, [rangesKey]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ── Leg management ── */

  const addLeg = useCallback((leg) => {
    const id = nextId++;
    setLegs((prev) => {
      const label = uniqueLegLabel(leg.label || `Leg ${id}`, prev);
      return [
        ...prev,
        {
          id,
          label,
          type: leg.type,           // "instrument", "continuous", or "option_stream"
          collection: leg.collection,
          symbol: leg.symbol || null,
          strategy: leg.strategy || null,
          adjustment: leg.adjustment || null,
          cycle: leg.cycle || null,
          rollOffset: leg.rollOffset ?? 0,
          // NTH_NEAREST continuous legs only: the rank-th nearest contract
          // (1 = front month). Defaults to 1 so non-nth_nearest legs are
          // unchanged. Sent to the compute/persist builders below.
          rank: leg.rank ?? 1,
          weight: leg.weight ?? 100,
          // option_stream fields (null for non-option legs)
          option_type: leg.option_type || null,
          maturity: leg.maturity || null,
          selection: leg.selection || null,
          stream: leg.stream || null,
          // option_stream roll offset — the unified {value, unit} object
          // (snake_case, matches OptionStreamForm + the OptionStreamRef wire
          // field; distinct from the futures leg's camelCase `rollOffset`).
          // null for non-option legs. ("End of month" is the maturity, not a
          // separate roll_schedule — that field was removed.)
          roll_offset: leg.roll_offset ?? null,
          // SELECT-AND-HOLD (fixed-contract dollar-P&L) — option_stream legs only.
          hold_between_rolls: leg.hold_between_rolls ?? false,
          nav_times: leg.nav_times ?? 1.0,
          // Option hold-mode SIZING (premium_notional default / futures_notional).
          // Must be preserved on the internal leg or the compute/persist builders
          // have nothing to forward and the leg silently falls back to
          // premium_notional (which wipes a low-premium leg to -100%).
          sizing_mode: leg.sizing_mode ?? null,
          futures_reference: leg.futures_reference ?? null,
        },
      ];
    });
    setDirty(true);
  }, []);

  const addSignalLeg = useCallback((signal) => {
    const id = nextId++;
    setLegs((prev) => {
      const label = uniqueLegLabel(signal.name || `Signal ${id}`, prev);
      return [
        ...prev,
        {
          id,
          label,
          type: 'signal',
          signalId: signal.id,
          signalName: signal.name,
          signalSpec: signal,
          weight: 100,
          collection: null,
          symbol: null,
          strategy: null,
          adjustment: null,
          cycle: null,
          rollOffset: 0,
        },
      ];
    });
    setDirty(true);
  }, []);

  const updateLeg = useCallback((index, updates) => {
    setLegs((prev) =>
      prev.map((leg, i) => (i === index ? { ...leg, ...updates } : leg)),
    );
    setDirty(true);
  }, []);

  const removeLeg = useCallback((index) => {
    setLegs((prev) => prev.filter((_, i) => i !== index));
    setDirty(true);
  }, []);

  const setRebalanceAndDirty = useCallback((value) => {
    setRebalance(value);
    setDirty(true);
  }, []);

  // Mark the current editor state as persisted — clears the ``dirty`` flag
  // WITHOUT touching legs/name/etc. Called by the page after a SUCCESSFUL
  // save (manual Save button AND debounced autosave). Without this the flag
  // was set true on every edit but reset only on load/clear, so the Save
  // button stayed solid and "Unsaved changes" persisted after a successful
  // save — the reported "Save does nothing" bug.
  const markSaved = useCallback(() => {
    setDirty(false);
  }, []);

  const clearAll = useCallback(() => {
    abortCalculate();
    setLegs([]);
    setResults(null);
    setError(null);
    setLegDateRanges({});
    setOverlapRange(null);
    setStartDate('');
    setEndDate('');
    setPortfolioName('');
    setDirty(false);
    setPersistedId(null);
    setPersistedCategory('RESEARCH');
    setPersistedLocked(false);
  }, [abortCalculate]);

  /**
   * Hydrate the editor from a backend-persisted portfolio doc. Replaces
   * legs / rebalance / name / persistedId. After this, the backend
   * autosave (managed at the page level) takes over.
   */
  const loadFromPersisted = useCallback((doc) => {
    if (!doc || typeof doc !== 'object') return;
    const backendLegs = Array.isArray(doc.legs) ? doc.legs : [];
    // Stamp local-only id onto each leg so React keys remain unique.
    // We do NOT round-trip the id back to the backend — the id is
    // assigned per-load, not stored.
    const restoredLegs = backendLegs.map((l) => {
      const leg = { ...l, id: nextId++ };
      // Backward-compat: an option PRICE leg (mid/bs_mid) is now hold-ON-only
      // (the backend rejects hold-off). A portfolio saved BEFORE that rule has
      // no hold_between_rolls, so coerce it on load — otherwise an old portfolio
      // loads fine but 400s on Compute with no in-UI way to enable hold.
      if (
        leg.type === 'option_stream'
        && (leg.stream === 'mid' || leg.stream === 'bs_mid')
      ) {
        leg.hold_between_rolls = true;
        if (typeof leg.nav_times !== 'number') leg.nav_times = 1.0;
      }
      return leg;
    });
    abortCalculate();
    setLegs(restoredLegs);
    setRebalance(typeof doc.rebalance === 'string' ? doc.rebalance : 'none');
    setPortfolioName(doc.name || '');
    setPersistedId(doc.id);
    setPersistedCategory(doc.category || 'RESEARCH');
    setPersistedLocked(!!doc.locked);
    setResults(null);
    setError(null);
    setLegDateRanges({});
    setOverlapRange(null);
    setStartDate('');
    setEndDate('');
    setDirty(false);
  }, [abortCalculate]);

  const setPersistedIdExternal = useCallback((id) => {
    setPersistedId(id);
  }, []);

  /* ── Calculate ── */

  const handleCalculate = useCallback(async () => {
    if (legs.length === 0) return;

    // Check for duplicate labels (dict keys would silently collapse)
    const labels = legs.map((l) => l.label);
    const duplicates = labels.filter((l, i) => labels.indexOf(l) !== i);
    if (duplicates.length > 0) {
      setError(`Duplicate leg labels: ${[...new Set(duplicates)].join(', ')}. Each leg must have a unique label.`);
      return;
    }

    // Effective compute window: an explicit slider selection takes priority;
    // otherwise fall back to the available range (the overlap of all legs,
    // including option legs which now resolve their real collection coverage).
    // This lets an option leg resolve over the portfolio's available window
    // without forcing a manual slider drag — the slider already renders '' as
    // the full range, so the request now matches what the user sees.
    const effectiveStart = startDate || overlapRange?.start;
    const effectiveEnd = endDate || overlapRange?.end;

    // Option stream legs require an explicit window (the backend can't infer
    // their date range). With the fallback above this is normally satisfied;
    // the guard remains as a safety net (e.g. ranges not yet settled).
    const hasOptionStreamLegs = legs.some((l) => l.type === 'option_stream');
    if (hasOptionStreamLegs && (!effectiveStart || !effectiveEnd)) {
      setError('Option stream legs require explicit start and end dates. Please set a date range.');
      return;
    }

    // Build the resolved compute body via the SHARED builder — the badge hashes
    // the exact same object, so the cache key is guaranteed identical (key
    // parity guardrail). Hydration is UNCONDITIONAL here (as on main) so the
    // cache-OFF path is byte-identical to today's — do not gate it on signal
    // legs or the cache flag.
    const availableIndicators = await hydrateAvailableIndicators();
    const { body, missingByLeg } = buildPortfolioComputeBody({
      legs,
      rebalance,
      start: effectiveStart,
      end: effectiveEnd,
      availableIndicators,
    });
    if (missingByLeg.length > 0) {
      const first = missingByLeg[0];
      setError(`Signal "${first.label}" references missing indicators: ${first.ids.join(', ')}. Please check the Indicators page.`);
      return;
    }

    // ── Compute = ALWAYS a fresh network run (never served from cache) ──
    // Serving cached results is now the auto-display effect's job; the button
    // always recomputes and (when the cache is on) RE-CACHES the fresh result.
    // The OFF path below is byte-for-byte today's (the cacheOn branches are
    // no-ops when disabled).
    const cacheOn = cacheEnabled;
    let cacheKey = null;
    if (cacheOn) {
      try {
        cacheKey = await computeCacheKey(body);
      } catch {
        cacheKey = null;
      }
      // Baseline the live-key mirror to the config we're about to compute so a
      // Compute clicked before the debounced effect resolved still displays; an
      // edit while in flight then moves currentKeyRef away (via the effect) and
      // FIX A drops the stale result.
      if (cacheKey) currentKeyRef.current = cacheKey;
    }

    // Race guards: mark a compute in flight and stamp a new sequence so the
    // auto-display effect can't overwrite the result this compute produces.
    computeSeqRef.current += 1;
    computingRef.current = true;

    setError(null);
    await runAbortable(async ({ signal }) => {
      try {
        const res = await computePortfolio({
          legs: body.legs,
          weights: body.weights,
          rebalance: body.rebalance,
          returnType: body.return_type,
          start: body.start,
          end: body.end,
          signal,
        });
        if (!signal.aborted) {
          // ALWAYS cache the fresh result — it is valid for the config it ran
          // for, so reverting to that config re-shows it (auto-display).
          if (cacheOn && cacheKey) {
            try {
              await putCached(cacheKey, persistedId, res);
            } catch {
              // caching is best-effort; the compute already succeeded
            }
          }
          // FIX A: only DISPLAY it if the live config still matches the one this
          // compute ran for. If the user edited mid-flight, drop it (stay blank
          // for the modified config); the cacheVersion bump below then re-syncs.
          if (shouldDisplayComputeResult({
            cacheOn,
            computeKey: cacheKey,
            liveKey: currentKeyRef.current,
          })) {
            setResults(res);
          }
        }
      } catch (err) {
        if (signal.aborted) return;
        setError(err.message || 'Computation failed');
      } finally {
        // Compute finished — release the in-flight guard, then (cache on) bump
        // the version so the auto-display/badge effect re-syncs to the newly
        // cached state (flips the badge to "Cached ✓").
        computingRef.current = false;
        if (cacheOn) setCacheVersion((v) => v + 1);
      }
    });
  }, [legs, rebalance, startDate, endDate, overlapRange, runAbortable, cacheEnabled, persistedId]);

  /* ── Reactive cache key + AUTO-DISPLAY / BLANK-ON-EDIT (cache-ON only) ── */
  // On any key-affecting change: recompute the current cache key (for the
  // badge) and then reflect the cache into the displayed results —
  //   HIT  → setResults(cached)  (auto-display, no Compute click)
  //   MISS → setResults(null)    (blank; the page shows "recompute needed")
  // Debounced so editing a numeric field doesn't hammer hydrate/hash on every
  // keystroke. Gated on a resolved date range so loading a cached portfolio
  // doesn't flash-blank before dates settle. This effect NEVER runs on the
  // cache-OFF path, so it cannot affect OFF fidelity.
  useEffect(() => {
    if (!cacheEnabled || legs.length === 0) {
      currentKeyRef.current = null;
      setCurrentCacheKey(null);
      return undefined;
    }
    const effStart = startDate || overlapRange?.start;
    const effEnd = endDate || overlapRange?.end;
    // Gate until the range resolves — do NOT blank here (avoid a flash on load).
    if (!effStart || !effEnd) {
      currentKeyRef.current = null;
      setCurrentCacheKey(null);
      return undefined;
    }
    let cancelled = false;
    const startSeq = computeSeqRef.current;
    const timer = setTimeout(async () => {
      try {
        // Hydration may be skipped when there are no signal legs — this effect
        // is new cache-ON-only code, so this does NOT affect OFF fidelity.
        const hasSignalLegs = legs.some((l) => l.type === 'signal');
        const availableIndicators = hasSignalLegs
          ? await hydrateAvailableIndicators()
          : [];
        const { body, missing } = buildPortfolioComputeBody({
          legs,
          rebalance,
          start: effStart,
          end: effEnd,
          availableIndicators,
        });
        if (missing.length > 0) {
          if (!cancelled) {
            currentKeyRef.current = null;
            setCurrentCacheKey(null);
          }
          return;
        }
        const key = await computeCacheKey(body);
        if (cancelled) return;
        // Update the live-key mirror FIRST (synchronously) so a compute landing
        // right now compares against the freshly-edited config (FIX A).
        currentKeyRef.current = key;
        setCurrentCacheKey(key);
        const cached = await getCached(key);
        // RACE GUARD: never overwrite results while a compute is in flight, and
        // never act on a read that a compute superseded (seq changed). The
        // compute's cacheVersion bump re-triggers this effect afterwards to
        // re-sync cleanly.
        if (cancelled || computingRef.current || computeSeqRef.current !== startSeq) {
          return;
        }
        setResults(cached ? cached : null);
      } catch {
        if (!cancelled) setCurrentCacheKey(null);
      }
    }, 275);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [cacheEnabled, legs, rebalance, startDate, endDate, overlapRange, cacheVersion]);

  const clearError = useCallback(() => setError(null), []);

  // Toggle autosave — no longer persisted to localStorage; the toggle
  // now controls the backend debounced autosave at the page level.
  // Old: localStorage.setItem(AUTOSAVE_KEY, String(on));
  const setAutosave = useCallback((on) => {
    setAutosaveState(on);
  }, []);

  return {
    legs,
    addLeg,
    addSignalLeg,
    updateLeg,
    removeLeg,
    clearAll,
    rebalance,
    setRebalance: setRebalanceAndDirty,
    dirty,
    markSaved,
    startDate,
    setStartDate,
    endDate,
    setEndDate,
    results,
    loading,
    legDateRanges,
    overlapRange,
    rangesLoading,
    error,
    clearError,
    handleCalculate,
    // Local portfolio-result cache (opt-in). ``cacheEnabled`` gates the badge
    // + auto-display; ``currentCacheKey`` is the active portfolio's key (null
    // while gated); ``cacheVersion`` bumps after each compute so the badge /
    // auto-display effect re-syncs.
    cacheEnabled,
    currentCacheKey,
    cacheVersion,
    // localStorage save/load functions — kept in the hook but no longer
    // exposed. All persistence goes through the backend now.
    // savePortfolio,
    // loadPortfolio,
    // deleteSavedPortfolio,
    // getSavedPortfolios,
    portfolioName,
    setPortfolioName,
    autosave,
    setAutosave,
    // Backend persistence wiring.
    persistedId,
    setPersistedId: setPersistedIdExternal,
    persistedCategory,
    setPersistedCategory,
    persistedLocked,
    setPersistedLocked,
    loadFromPersisted,
  };
}
