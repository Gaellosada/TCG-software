import { useState, useCallback, useMemo, useEffect } from 'react';
import { computePortfolio } from '../../api/portfolio';
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import { formatDateInt } from '../../utils/format';
import { useAutosave } from '../../components/SaveControls';
import { buildComputeRequestBody } from '../Signals/requestBuilder';
import { hydrateAvailableIndicators } from '../Signals/hydrateIndicators';
import { fetchSignalLegRange } from './signalLegRange';
import { legsToRangesKey } from './legKey';
import {
  savePortfolio as persistPortfolio,
  loadPortfolio as loadPortfolioEntry,
  deleteSavedPortfolio as removeSavedPortfolio,
  getSavedPortfolios as listSavedPortfolios,
} from './storage';
import useAbortableAction from '../../hooks/useAbortableAction';

const AUTOSAVE_KEY = 'tcg-portfolio-autosave';

let nextId = 1;

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
  const [autosave, setAutosaveState] = useState(
    () => localStorage.getItem(AUTOSAVE_KEY) === 'true',
  );

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
      // Auto-suffix to avoid duplicate labels (keys would collapse in the API dict).
      let label = leg.label || `Leg ${id}`;
      const existing = new Set(prev.map((l) => l.label));
      if (existing.has(label)) {
        let n = 2;
        while (existing.has(`${label} (${n})`)) n++;
        label = `${label} (${n})`;
      }
      return [
        ...prev,
        {
          id,
          label,
          type: leg.type,           // "instrument" or "continuous"
          collection: leg.collection,
          symbol: leg.symbol || null,
          strategy: leg.strategy || null,
          adjustment: leg.adjustment || null,
          cycle: leg.cycle || null,
          rollOffset: leg.rollOffset ?? 0,
          weight: leg.weight ?? 100,
        },
      ];
    });
    setDirty(true);
  }, []);

  const addSignalLeg = useCallback((signal) => {
    const id = nextId++;
    setLegs((prev) => {
      // Auto-suffix to avoid duplicate labels (keys would collapse in the API dict).
      let label = signal.name || `Signal ${id}`;
      const existing = new Set(prev.map((l) => l.label));
      if (existing.has(label)) {
        let n = 2;
        while (existing.has(`${label} (${n})`)) n++;
        label = `${label} (${n})`;
      }
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
  }, [abortCalculate]);

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

    // Build legs dict for API
    const availableIndicators = hydrateAvailableIndicators();
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

  /* ── Save/Load (localStorage) ── */

  const savePortfolio = useCallback(
    (name) => {
      persistPortfolio(name, { legs, rebalance });
      setPortfolioName(name);
      setDirty(false);
    },
    [legs, rebalance],
  );

  const loadPortfolio = useCallback((name) => {
    const entry = loadPortfolioEntry(name);
    if (!entry) return false;

    const restoredLegs = (entry.legs || []).map((l) => ({
      ...l,
      id: nextId++,
    }));

    setLegs(restoredLegs);
    if (entry.rebalance) setRebalance(entry.rebalance);
    setStartDate('');
    setEndDate('');
    setResults(null);
    setError(null);
    setPortfolioName(name);
    setDirty(false);
    return true;
  }, []);

  const deleteSavedPortfolio = useCallback((name) => {
    removeSavedPortfolio(name);
  }, []);

  const getSavedPortfolios = useCallback(() => listSavedPortfolios(), []);

  const setAutosave = useCallback((on) => {
    setAutosaveState(on);
    localStorage.setItem(AUTOSAVE_KEY, String(on));
  }, []);

  /* ── Autosave: shared useAutosave hook ── */
  // Payload identity changes when any persisted field does; the hook
  // debounces writes + flushes on beforeunload/pagehide.
  const autosavePayload = useMemo(
    () => ({ legs, rebalance, name: portfolioName }),
    [legs, rebalance, portfolioName],
  );
  const autosaveEnabled = !!autosave && !!portfolioName && legs.length > 0;
  const handleAutosave = useCallback(
    () => { if (portfolioName) savePortfolio(portfolioName); },
    [portfolioName, savePortfolio],
  );
  useAutosave({
    enabled: autosaveEnabled,
    dirty,
    value: autosavePayload,
    onSave: handleAutosave,
    debounceMs: 500,
  });

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
    savePortfolio,
    loadPortfolio,
    deleteSavedPortfolio,
    getSavedPortfolios,
    portfolioName,
    autosave,
    setAutosave,
  };
}
