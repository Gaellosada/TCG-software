import { useState, useCallback, useMemo, useRef, useEffect } from 'react';
import { computePortfolio } from '../../api/portfolio';
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import { formatDateInt } from '../../utils/format';
import { useAutosave } from '../../components/SaveControls';
import { buildComputeRequestBody } from '../Signals/requestBuilder';
import { hydrateAvailableIndicators } from '../Signals/hydrateIndicators';

const STORAGE_KEY = 'tcg-saved-portfolios';
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [legDateRanges, setLegDateRanges] = useState({});
  const [overlapRange, setOverlapRange] = useState(null);
  const [rangesLoading, setRangesLoading] = useState(false);
  const [portfolioName, setPortfolioName] = useState('');
  const [dirty, setDirty] = useState(false);
  const [autosave, setAutosaveState] = useState(
    () => localStorage.getItem(AUTOSAVE_KEY) === 'true',
  );

  const abortRef = useRef(null);

  /* ── Helpers ── */

  /**
   * Fetch the date range for a signal leg by fetching each input's instrument
   * range and computing the overlap (latest start, earliest end).
   */
  async function fetchSignalLegRange(leg) {
    const inputs = leg.signalSpec?.inputs || [];
    const configured = inputs.filter((inp) => inp.instrument);
    if (configured.length === 0) {
      return { id: leg.id, start: null, end: null };
    }

    const inputRanges = await Promise.all(
      configured.map(async (inp) => {
        try {
          let dates;
          const inst = inp.instrument;
          if (inst.type === 'continuous') {
            const res = await getContinuousSeries(inst.collection, {
              strategy: inst.strategy || 'front_month',
              adjustment: inst.adjustment || 'none',
              cycle: inst.cycle || undefined,
              rollOffset: inst.rollOffset || 0,
            });
            dates = res?.dates;
          } else {
            const res = await getInstrumentPrices(
              inst.collection,
              inst.instrument_id || inst.symbol,
            );
            dates = res?.dates;
          }
          if (dates && dates.length > 0) {
            return {
              start: formatDateInt(dates[0]),
              end: formatDateInt(dates[dates.length - 1]),
            };
          }
          return null;
        } catch {
          return null;
        }
      }),
    );

    const valid = inputRanges.filter(Boolean);
    if (valid.length === 0) {
      return { id: leg.id, start: null, end: null };
    }

    // Overlap = latest start, earliest end
    const start = valid.reduce((a, b) => (a.start > b.start ? a : b)).start;
    const end = valid.reduce((a, b) => (a.end < b.end ? a : b)).end;

    if (start <= end) {
      return { id: leg.id, start, end };
    }
    return { id: leg.id, start: null, end: null };
  }

  /* ── Fetch date ranges when legs change ── */

  // Stable key: only data-affecting fields (not label/weight) trigger re-fetch
  const rangesKey = useMemo(
    () => legs.map((l) => {
      if (l.type === 'signal') {
        // Include input instruments so re-binding triggers a refetch
        const inputKeys = (l.signalSpec?.inputs || []).map((inp) => {
          const inst = inp.instrument;
          if (!inst) return 'null';
          if (inst.type === 'continuous') return `c:${inst.collection}:${inst.strategy}:${inst.adjustment}:${inst.cycle}:${inst.rollOffset}`;
          return `i:${inst.collection}:${inst.instrument_id}`;
        }).join(',');
        return `s:${l.signalId}:[${inputKeys}]`;
      }
      if (l.type === 'continuous') return `c:${l.collection}:${l.strategy}:${l.adjustment}:${l.cycle}:${l.rollOffset}`;
      return `i:${l.collection}:${l.symbol}`;
    }).join('|'),
    [legs],
  );

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
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setLegs([]);
    setResults(null);
    setError(null);
    setLoading(false);
    setLegDateRanges({});
    setOverlapRange(null);
    setStartDate('');
    setEndDate('');
    setPortfolioName('');
    setDirty(false);
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

    // Abort previous request
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setError(null);

    try {
      const res = await computePortfolio({
        legs: apiLegs,
        weights: apiWeights,
        rebalance,
        returnType: 'normal',
        start: startDate || undefined,
        end: endDate || undefined,
        signal: controller.signal,
      });
      if (!controller.signal.aborted) {
        setResults(res);
        setLoading(false);
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      setError(err.message || 'Computation failed');
      setLoading(false);
    }
  }, [legs, rebalance, startDate, endDate]);

  const clearError = useCallback(() => setError(null), []);

  /* ── Save/Load (localStorage) ── */

  const savePortfolio = useCallback(
    (name) => {
      let saved;
      try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
      catch { saved = {}; }
      const weightsDict = {};
      for (const l of legs) weightsDict[l.label] = Number(l.weight) || 0;
      saved[name] = {
        legs: legs.map((l) => ({
          label: l.label,
          type: l.type,
          collection: l.collection,
          symbol: l.symbol,
          strategy: l.strategy,
          adjustment: l.adjustment,
          cycle: l.cycle,
          rollOffset: l.rollOffset,
          weight: l.weight,
          // Signal-specific fields (null for non-signal legs).
          signalId: l.signalId || null,
          signalName: l.signalName || null,
          signalSpec: l.signalSpec || null,
        })),
        weights: weightsDict,
        rebalance,
        savedAt: new Date().toISOString(),
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
      setPortfolioName(name);
      setDirty(false);
    },
    [legs, rebalance],
  );

  const loadPortfolio = useCallback((name) => {
    let saved;
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
    catch { saved = {}; }
    const entry = saved[name];
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
    let saved;
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
    catch { saved = {}; }
    delete saved[name];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
  }, []);

  const getSavedPortfolios = useCallback(() => {
    let saved;
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
    catch { saved = {}; }
    return Object.keys(saved);
  }, []);

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
