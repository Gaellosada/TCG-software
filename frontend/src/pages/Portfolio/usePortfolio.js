import { useState, useCallback, useMemo, useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { computePortfolio } from '../../api/portfolio';
import { getPortfolio } from '../../api/persistence';
import { queryKeys } from '../../queryKeys';
import { hydrateAvailableIndicators } from '../Signals/hydrateIndicators';
import { legsToRangesKey } from './legKey';
import { resolvePortfolioRange } from './resolvePortfolioRange';
import { persistedDocToLegs } from './persistedDoc';
import { buildPortfolioComputeBody } from './computeBodyBuilder';
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

  /* ── Fetch date ranges when legs change ── */

  // Re-resolve ranges when the range SPEC changes (legsToRangesKey — instrument
  // / roll / etc., NOT label/weight) OR when the leg IDENTITY set changes
  // (load / add / remove; ids are stable across weight/label edits). Including
  // the ids fixes a stuck-null overlapRange when switching to a different
  // portfolio that happens to share an identical range spec (same instrument,
  // different weight): the spec key alone wouldn't change, so the effect
  // wouldn't re-fire and overlapRange (nulled on load) would stay null —
  // sending start=undefined to Compute. Weight/label edits keep the same ids
  // AND spec, so they still never trigger a refetch (the original optimization
  // holds).
  const rangesKey = useMemo(
    () => `${legsToRangesKey(legs)}#${legs.map((l) => l.id).join(',')}`,
    [legs],
  );

  useEffect(() => {
    if (legs.length === 0) {
      setLegDateRanges({});
      setOverlapRange(null);
      return undefined;
    }

    let cancelled = false;
    setRangesLoading(true);

    // Resolve each leg's coverage + the portfolio overlap via the SHARED
    // resolver (also used to seed the compute window).
    resolvePortfolioRange(legs, { queryClient })
      .then(({ ranges, overlapRange: overlap }) => {
        if (cancelled) return;
        setLegDateRanges(ranges);
        setOverlapRange(overlap);
        setRangesLoading(false);
      })
      .catch(() => {
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

  // Add a saved PURE portfolio as a composed leg (mirrors addSignalLeg). We
  // store ONLY enough to render the row (id + name) and the weight — the FULL
  // child spec is resolved FRESH at compute time by ``resolvePortfolio`` below,
  // NEVER snapshotted here, so editing the child propagates (live reference).
  const addPortfolioLeg = useCallback((child) => {
    const id = nextId++;
    setLegs((prev) => {
      const label = uniqueLegLabel(child.name || `Portfolio ${id}`, prev);
      return [
        ...prev,
        {
          id,
          label,
          type: 'portfolio',
          portfolioId: child.id,
          portfolioName: child.name,
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
    // Shared doc→legs conversion (incl. the option hold-ON coercion), then stamp
    // a local-only React-key id. The id is assigned per-load, never round-tripped
    // to the backend.
    const restoredLegs = persistedDocToLegs(doc).map((l) => ({ ...l, id: nextId++ }));
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

  /* ── Composed portfolios: resolve child (sub-portfolio) legs ── */
  // The distinct set of referenced child portfolio ids (composed page only —
  // pure portfolios have none, so all of this is inert on the pure path).
  const portfolioLegIds = useMemo(
    () => [...new Set(
      legs.filter((l) => l.type === 'portfolio' && l.portfolioId).map((l) => l.portfolioId),
    )],
    [legs],
  );
  const portfolioLegIdsKey = portfolioLegIds.join(',');

  // childPortfolios: { [id]: doc | 'broken' } for the per-leg broken-ref badge.
  // Undefined (absent) = still loading. Populated by fetching each child's
  // CURRENT saved doc through React Query (deduped with the build-time resolver
  // below, which reads the SAME cache entries).
  const [childPortfolios, setChildPortfolios] = useState({});
  useEffect(() => {
    if (portfolioLegIds.length === 0) {
      setChildPortfolios({});
      return undefined;
    }
    let cancelled = false;
    Promise.all(portfolioLegIds.map(async (id) => {
      try {
        const doc = await queryClient.fetchQuery({
          queryKey: queryKeys.persistence.portfolios.detail(id),
          queryFn: () => getPortfolio(id),
          staleTime: 10 * 1000,
        });
        return [id, doc || 'broken'];
      } catch {
        return [id, 'broken'];
      }
    })).then((entries) => {
      if (!cancelled) setChildPortfolios(Object.fromEntries(entries));
    });
    return () => { cancelled = true; };
  }, [portfolioLegIdsKey, queryClient]); // eslint-disable-line react-hooks/exhaustive-deps

  // A resolved child doc is USABLE only if it exists, still has legs, and is not
  // archived/deleted — otherwise it's a broken reference (design §5). Shared by
  // the sync UI resolver and the async build-time resolver so both agree.
  const usableChildDoc = useCallback((doc) => {
    if (!doc || doc === 'broken') return null;
    if (doc.category === 'ARCHIVE' || doc.category === 'DELETED') return null;
    if (!Array.isArray(doc.legs) || doc.legs.length === 0) return null;
    return doc;
  }, []);

  // Sync resolver over the fetched-into-state child docs — drives the per-leg
  // broken-ref badge (portfolioRefStatus). NOT used for building bodies (that
  // uses the always-fresh async resolver below) to avoid a state/race gap.
  const resolvePortfolio = useCallback(
    (id) => usableChildDoc(childPortfolios[id]),
    [childPortfolios, usableChildDoc],
  );

  // Per-leg reference status for the Holdings UI: 'loading' | 'ok' | 'broken'.
  const portfolioRefStatus = useMemo(() => {
    const out = {};
    for (const l of legs) {
      if (l.type !== 'portfolio') continue;
      const doc = childPortfolios[l.portfolioId];
      if (doc === undefined) out[l.id] = 'loading';
      else out[l.id] = usableChildDoc(doc) ? 'ok' : 'broken';
    }
    return out;
  }, [legs, childPortfolios, usableChildDoc]);

  // Build-time resolver: fetch every referenced child's CURRENT spec through
  // React Query (deduped; same cache the state effect fills) and return a SYNC
  // ``(id) => doc|null`` closure over that fresh snapshot. ``handleCalculate``
  // awaits this to inline each child's CURRENT spec, so a child edit flows
  // straight into the compute body — and, because the backend result cache is
  // content-addressed on that inlined body, a child edit produces a new key →
  // recompute (live-reference invalidation is preserved by the inlining).
  const resolveChildrenNow = useCallback(async () => {
    if (portfolioLegIds.length === 0) return () => null;
    const pairs = await Promise.all(portfolioLegIds.map(async (id) => {
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
    return (id) => usableChildDoc(map[id]);
  }, [portfolioLegIdsKey, queryClient, usableChildDoc]); // eslint-disable-line react-hooks/exhaustive-deps

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

    // Build the resolved compute body via the SHARED builder. Hydration is
    // unconditional so signal legs (including ones inside a referenced child)
    // always resolve their indicators.
    const availableIndicators = await hydrateAvailableIndicators();
    // Resolve every referenced child portfolio's CURRENT spec (composed page).
    // On the pure page there are none, so this returns a no-op resolver and the
    // built body is byte-identical to today's.
    const resolveChild = await resolveChildrenNow();
    const { body, missingByLeg, brokenRefs } = buildPortfolioComputeBody({
      legs,
      rebalance,
      start: effectiveStart,
      end: effectiveEnd,
      availableIndicators,
      resolvePortfolio: resolveChild,
    });
    if (missingByLeg.length > 0) {
      const first = missingByLeg[0];
      setError(`Signal "${first.label}" references missing indicators: ${first.ids.join(', ')}. Please check the Indicators page.`);
      return;
    }
    if (brokenRefs.length > 0) {
      const first = brokenRefs[0];
      setError(`Portfolio leg "${first.label}" references a portfolio that can't be resolved (deleted, archived, or empty). Remove it or pick another building block.`);
      return;
    }

    // Compute is a single fresh network run and displays the result. The
    // backend serves from its on-disk result cache transparently (the response
    // carries ``from_cache``); the frontend keeps no cache of its own.
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
        if (!signal.aborted) setResults(res);
      } catch (err) {
        if (signal.aborted) return;
        setError(err.message || 'Computation failed');
      }
    });
  }, [legs, rebalance, startDate, endDate, overlapRange, runAbortable, resolveChildrenNow]);

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
    // Composed portfolios: add a saved PURE portfolio as a leg; ``resolvePortfolio``
    // + ``portfolioRefStatus`` drive live child resolution and the broken-ref UI.
    addPortfolioLeg,
    resolvePortfolio,
    portfolioRefStatus,
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
