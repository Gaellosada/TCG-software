import { useCallback, useState } from 'react';
import useAbortableAction from '../../hooks/useAbortableAction';
import { getOptionChain } from '../../api/options';

/**
 * Returns the ISO date string (YYYY-MM-DD) for today in local time.
 */
function todayISO() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

/**
 * Returns the ISO date string for today + ``days`` calendar days in local time.
 */
function addDays(isoDate, days) {
  const d = new Date(`${isoDate}T00:00:00`);
  d.setDate(d.getDate() + days);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function buildDefaultFilters(initialRoot) {
  const today = todayISO();
  return {
    root: initialRoot,
    date: today,
    type: 'both',
    expirationMin: today,
    expirationMax: addDays(today, 90),
    strikeMin: null,
    strikeMax: null,
    // Decision C: computeMissing defaults to false; never persisted to localStorage.
    computeMissing: false,
  };
}

/**
 * Feature hook owning options chain query state.
 *
 * ``fetchChain`` is intentionally explicit — the caller controls when to
 * trigger a fetch (on mount, on filter-change-debounced, etc.).  This avoids
 * the anti-pattern of fetching on every state update.
 *
 * The typical caller pattern:
 *   updateFilters({ root: 'OPT_SP_500' });
 *   // then in a debounced effect or explicit button handler:
 *   fetchChain();
 *
 * Abort safety: calling ``fetchChain`` while a previous request is in-flight
 * automatically cancels the earlier request via ``useAbortableAction``.
 * ``AbortError`` is swallowed silently; other errors are surfaced as
 * ``{ error }`` on ``chainData``.
 *
 * Decision C: ``computeMissing`` is transient local state — never persisted
 * to localStorage.  State resets on every remount.
 *
 * @param {string|null} initialRoot - Optional root to pre-populate (e.g. 'OPT_SP_500').
 * @returns {{
 *   filters: object,
 *   chainData: null | { error: Error } | object,
 *   loading: boolean,
 *   fetchChain: () => Promise<void>,
 *   updateFilters: (partial: object) => void,
 *   abort: () => void,
 * }}
 */
export function useOptionsChain(initialRoot = null) {
  const [filters, setFilters] = useState(() => buildDefaultFilters(initialRoot));
  const [chainData, setChainData] = useState(null);

  const { run, running, abort } = useAbortableAction();

  /**
   * Fetch the chain for the current ``filters`` snapshot at call-time.
   * Any in-flight request is cancelled before the new one starts
   * (handled internally by ``useAbortableAction.run``).
   */
  const fetchChain = useCallback(async () => {
    if (!filters.root || !filters.date) return;
    try {
      const data = await run(({ signal }) =>
        getOptionChain(filters.root, {
          date: filters.date,
          type: filters.type,
          expirationMin: filters.expirationMin,
          expirationMax: filters.expirationMax,
          strikeMin: filters.strikeMin,
          strikeMax: filters.strikeMax,
          computeMissing: filters.computeMissing,
          signal,
        }),
      );
      setChainData(data);
    } catch (err) {
      if (err && err.name === 'AbortError') {
        // Silently swallow — a newer request superseded this one.
        return;
      }
      setChainData({ error: err });
    }
  }, [filters, run]);

  /**
   * Merge a partial object into current filters.  Caller is responsible for
   * calling ``fetchChain()`` afterward if an immediate re-fetch is desired.
   */
  const updateFilters = useCallback((partial) => {
    setFilters((prev) => ({ ...prev, ...partial }));
  }, []);

  return {
    filters,
    /** null until first fetch; { error: Error } on failure; ChainResponse on success. */
    chainData,
    loading: running,
    fetchChain,
    updateFilters,
    abort,
  };
}
