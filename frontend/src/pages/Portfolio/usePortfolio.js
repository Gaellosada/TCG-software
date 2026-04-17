import { useState, useCallback, useMemo, useRef, useEffect } from 'react';
import { computePortfolio } from '../../api/portfolio';
import { getInstrumentPrices, getContinuousSeries } from '../../api/data';
import { formatDateInt } from '../../utils/format';

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
  const autosaveTimerRef = useRef(null);

  /* ── Fetch date ranges when legs change ── */

  // Stable key: only data-affecting fields (not label/weight) trigger re-fetch
  const rangesKey = useMemo(
    () => legs.map((l) =>
      l.type === 'continuous'
        ? `c:${l.collection}:${l.strategy}:${l.adjustment}:${l.cycle}:${l.rollOffset}`
        : `i:${l.collection}:${l.symbol}`
    ).join('|'),
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

    // Fetch each leg's price data using the same APIs as the Data page
    const promises = legs.map(async (leg) => {
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
            label: leg.label,
            start: formatDateInt(dates[0]),
            end: formatDateInt(dates[dates.length - 1]),
          };
        }
        return { label: leg.label, start: null, end: null };
      } catch {
        return { label: leg.label, start: null, end: null };
      }
    });

    Promise.all(promises).then((results) => {
      if (cancelled) return;

      const ranges = {};
      const validStarts = [];
      const validEnds = [];

      for (const r of results) {
        ranges[r.label] = { start: r.start, end: r.end };
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
    });

    return () => { cancelled = true; };
  }, [rangesKey]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ── Leg management ── */

  const addLeg = useCallback((leg) => {
    const id = nextId++;
    setLegs((prev) => [
      ...prev,
      {
        id,
        label: leg.label || `Leg ${id}`,
        type: leg.type,           // "instrument" or "continuous"
        collection: leg.collection,
        symbol: leg.symbol || null,
        strategy: leg.strategy || null,
        adjustment: leg.adjustment || null,
        cycle: leg.cycle || null,
        rollOffset: leg.rollOffset ?? 0,
        weight: leg.weight ?? 100,
      },
    ]);
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

  /* ── Derived weights ── */

  const weights = useMemo(
    () => legs.reduce((acc, leg) => {
      acc[leg.label] = Number(leg.weight) || 0;
      return acc;
    }, {}),
    [legs],
  );

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
    const apiLegs = {};
    for (const leg of legs) {
      if (leg.type === 'continuous') {
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
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
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
        })),
        weights,
        rebalance,
        savedAt: new Date().toISOString(),
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
      setPortfolioName(name);
      setDirty(false);
    },
    [legs, weights, rebalance],
  );

  const loadPortfolio = useCallback((name) => {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
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
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    delete saved[name];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
  }, []);

  const getSavedPortfolios = useCallback(() => {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    return Object.keys(saved);
  }, []);

  const setAutosave = useCallback((on) => {
    setAutosaveState(on);
    localStorage.setItem(AUTOSAVE_KEY, String(on));
  }, []);

  /* ── Autosave: debounced save when portfolio changes ── */

  useEffect(() => {
    if (!autosave || !portfolioName || legs.length === 0 || !dirty) return;

    // Debounce: save 500ms after last change
    clearTimeout(autosaveTimerRef.current);
    autosaveTimerRef.current = setTimeout(() => {
      savePortfolio(portfolioName);
    }, 500);

    return () => clearTimeout(autosaveTimerRef.current);
  }, [autosave, portfolioName, legs, rebalance, dirty, savePortfolio]);

  return {
    legs,
    addLeg,
    updateLeg,
    removeLeg,
    clearAll,
    weights,
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
