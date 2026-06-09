import { useState, useCallback, useMemo, useEffect } from 'react';
import { computePortfolio } from '../../api/portfolio';
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import { formatDateInt } from '../../utils/format';
import { buildComputeRequestBody } from '../Signals/requestBuilder';
import { hydrateAvailableIndicators } from '../Signals/hydrateIndicators';
import { fetchSignalLegRange } from './signalLegRange';
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
        // Option streams need materialisation to know exact date bounds.
        // Return null dates — the user must set explicit start/end dates.
        return { id: leg.id, start: null, end: null };
      }
      try {
        let dates;
        if (leg.type === 'continuous') {
          const res = await getContinuousSeries(leg.collection, {
            strategy: leg.strategy || 'front_month',
            adjustment: leg.adjustment || 'none',
            cycle: leg.cycle || undefined,
            rollOffset: leg.rollOffset || 0,
          });
          dates = res?.dates;
        } else {
          const res = await getInstrumentPrices(leg.collection, leg.symbol);
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
    const restoredLegs = backendLegs.map((l) => ({ ...l, id: nextId++ }));
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

    // Option stream legs require explicit dates (the backend can't infer range)
    const hasOptionStreamLegs = legs.some((l) => l.type === 'option_stream');
    if (hasOptionStreamLegs && (!startDate || !endDate)) {
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
          start: startDate || undefined,
          end: endDate || undefined,
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
  }, [legs, rebalance, startDate, endDate, runAbortable]);

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
