import { useState, useCallback, useMemo, useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { computePortfolio } from '../../api/portfolio';
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import { queryKeys } from '../../queryKeys';
import { formatDateInt } from '../../utils/format';
import { buildComputeRequestBody } from '../Signals/requestBuilder';
import { hydrateAvailableIndicators } from '../Signals/hydrateIndicators';
import { fetchSignalLegRange } from './signalLegRange';
import { fetchOptionLegRange } from './optionLegRange';
import { legsToRangesKey } from './legKey';
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

    // Build legs dict for API
    const availableIndicators = await hydrateAvailableIndicators();
    const apiLegs = {};
    for (const leg of legs) {
      if (leg.type === 'signal') {
        const { body, missing } = buildComputeRequestBody(leg.signalSpec, availableIndicators);
        if (missing.length > 0) {
          setError(`Signal "${leg.label}" references missing indicators: ${missing.join(', ')}. Please check the Indicators page.`);
          return;
        }
        apiLegs[leg.label] = {
          type: 'signal',
          signal_spec: body,
        };
      } else if (leg.type === 'option_stream') {
        apiLegs[leg.label] = {
          type: 'option_stream',
          collection: leg.collection,
          option_type: leg.option_type,
          cycle: leg.cycle,
          maturity: leg.maturity,
          selection: leg.selection,
          stream: leg.stream,
        };
        // SELECT-AND-HOLD (fixed-contract dollar-P&L). An option PRICE leg
        // (mid/bs_mid) is hold-ON-only — the backend rejects hold-off — so ALWAYS
        // send hold for a premium leg, which also covers legacy legs persisted
        // before that rule (otherwise the whole /compute request 400s). Level
        // streams (iv/greeks) never carry hold.
        const isPremiumLeg = leg.stream === 'mid' || leg.stream === 'bs_mid';
        if (isPremiumLeg || leg.hold_between_rolls) {
          apiLegs[leg.label].hold_between_rolls = true;
          apiLegs[leg.label].nav_times = leg.nav_times ?? 1.0;
          // SIZING mode for the hold-mode $-P&L. Send ``futures_notional`` (size
          // off the underlying future's notional) + its reference future ONLY
          // when chosen — a premium-notional leg stays byte-identical and the
          // backend applies its default. Without this the compute request always
          // ran premium_notional, wiping a low-premium (e.g. 10Δ) leg to -100%.
          if (leg.sizing_mode === 'futures_notional') {
            apiLegs[leg.label].sizing_mode = 'futures_notional';
            apiLegs[leg.label].futures_reference =
              leg.futures_reference || 'nearest_on_or_after';
          }
        }
        // Roll offset is the unified {value, unit} object — send it only when
        // its value is non-zero (omit the no-op to keep the body minimal; the
        // BE defaults to value 0). Option streams carry NO back-adjustment, so
        // no `adjustment` is sent. ("End of month" is the maturity, not a
        // separate roll_schedule — that field was removed.)
        const ro = leg.roll_offset;
        if (ro && typeof ro === 'object' && ro.value > 0) {
          apiLegs[leg.label].roll_offset = { value: ro.value, unit: ro.unit || 'days' };
        } else if (typeof ro === 'number' && ro > 0) {
          // Legacy in-memory int (days) — forward in the unified shape.
          apiLegs[leg.label].roll_offset = { value: ro, unit: 'days' };
        }
      } else if (leg.type === 'continuous') {
        apiLegs[leg.label] = {
          type: 'continuous',
          collection: leg.collection,
          strategy: leg.strategy || 'front_month',
          adjustment: leg.adjustment || 'none',
        };
        if (leg.cycle) {
          apiLegs[leg.label].cycle = leg.cycle;
        }
        if (leg.rollOffset > 0) {
          apiLegs[leg.label].roll_offset = leg.rollOffset;
        }
      } else {
        apiLegs[leg.label] = {
          type: 'instrument',
          collection: leg.collection,
          symbol: leg.symbol,
        };
      }
    }

    const apiWeights = {};
    for (const leg of legs) {
      apiWeights[leg.label] = Number(leg.weight) || 0;
    }

    setError(null);
    await runAbortable(async ({ signal }) => {
      try {
        const res = await computePortfolio({
          legs: apiLegs,
          weights: apiWeights,
          rebalance,
          returnType: 'normal',
          start: effectiveStart || undefined,
          end: effectiveEnd || undefined,
          signal,
        });
        if (!signal.aborted) {
          setResults(res);
        }
      } catch (err) {
        if (signal.aborted) return;
        setError(err.message || 'Computation failed');
      }
    });
  }, [legs, rebalance, startDate, endDate, overlapRange, runAbortable]);

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
