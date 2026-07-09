/**
 * Market-data query hooks (stale-while-revalidate).
 *
 * Each hook wraps TanStack ``useQuery`` for one market-data read and returns
 * the SAME ``{ data, loading, error }`` shape the components already consumed
 * from ``useAsync`` — so migrating a caller is a one-line swap with ZERO change
 * to the surrounding JSX/UI. This is deliberate: the brief forbids
 * restructuring components beyond what SWR needs.
 *
 * Why these are real cache reads (not the old useEffect+fetch):
 *   - On first mount the query runs and the component shows its loading state
 *     once (no cache yet — acceptable).
 *   - On every subsequent mount (navigating back to the page) the cached data
 *     is returned synchronously → the component renders data immediately with
 *     NO loading flash. If the entry is stale, a background refetch runs and
 *     silently patches the view via TanStack's structural sharing (unchanged
 *     rows keep referential identity, so memoised children don't re-render).
 *
 * Loading semantics:
 *   We surface ``isPending`` as ``loading``. ``isPending`` is true ONLY when
 *   there is no cached data yet (first-ever load). It is FALSE on a warm-cache
 *   remount even while a background refetch is in flight (that's ``isFetching``,
 *   intentionally NOT surfaced — a background revalidate must not show a
 *   spinner). This is precisely the no-spinner-on-navigation behaviour.
 *
 * Error shape:
 *   The api client raises ``ApiError`` / ``FetchError`` (Error subclasses) with
 *   a ``.message``. Components read ``error.message``; TanStack stores the
 *   thrown error verbatim on ``error``, so the shape is preserved.
 *
 * AbortSignal:
 *   TanStack passes an ``AbortSignal`` to the queryFn context. We thread it
 *   into api functions that accept ``{ signal }`` so a superseded fetch is
 *   cancelled — same guarantee the old AbortController code gave.
 */
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { queryKeys } from '../queryKeys';
import {
  listCollections,
  listInstruments,
  getInstrumentPrices,
  getContinuousSeries,
  getAvailableCycles,
  getBasketSeries,
} from '../api/data';
import {
  getOptionRoots,
  getOptionExpirations,
  getOptionContract,
  getChainSnapshot,
} from '../api/options';

/**
 * Normalise a TanStack query result into the legacy ``{ data, loading, error }``
 * triple the components expect.
 *
 * ``loading`` ← ``isPending`` (no cached data yet). On a warm remount this is
 * false even during a background refetch — that is the whole point.
 * ``data`` falls back to ``null`` (matching useAsync's initial ``data: null``)
 * so existing ``if (!data)`` guards behave identically.
 */
function asAsyncResult(query) {
  // A *disabled* query (enabled:false) sits at status:'pending' +
  // fetchStatus:'idle' — it is not actually loading. The old useAsync path,
  // when its guard short-circuited to ``Promise.resolve(null)``, settled to
  // ``{ data: null, loading: false }``. Mirror that: only report ``loading``
  // when a fetch is genuinely pending (no data yet AND actively fetching).
  // On a warm-cache remount ``isPending`` is already false, so a background
  // refetch never flips ``loading`` true — that is the no-spinner guarantee.
  const loading = query.isPending && query.fetchStatus !== 'idle';
  return {
    data: query.data ?? null,
    loading,
    error: query.error ?? null,
    // Extra fields available to callers that want them; ignoring them keeps
    // the legacy shape intact.
    isFetching: query.isFetching,
    refetch: query.refetch,
  };
}

/** Collections list (optionally filtered by asset class). */
export function useCollections(assetClass = null, options = {}) {
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.collections(assetClass),
      queryFn: ({ signal }) => listCollections(assetClass, { signal }),
      ...options,
    }),
  );
}

/** Paginated instruments for a collection. Disabled when collection is falsy. */
export function useInstruments(collection, { skip = 0, limit = 50, ...options } = {}) {
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.instruments(collection, skip, limit),
      queryFn: ({ signal }) => listInstruments(collection, { skip, limit, signal }),
      enabled: !!collection && (options.enabled ?? true),
      ...options,
    }),
  );
}

/** Price series for a single instrument. */
export function useInstrumentPrices(collection, instrument, options = {}) {
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.prices(collection, instrument),
      queryFn: () => getInstrumentPrices(collection, instrument),
      enabled: !!collection && !!instrument && (options.enabled ?? true),
      ...options,
    }),
  );
}

/**
 * Continuous (rolled) series for a futures collection.
 *
 * ``placeholderData: keepPreviousData`` so when the user changes an adjustment
 * / cycle / roll-offset control, the previous series stays on screen while the
 * new one loads — no flash to a loading state mid-interaction.
 */
export function useContinuousSeries(collection, params = {}, options = {}) {
  const { strategy = 'front_month', adjustment = 'none', cycle, rollOffset, rank = 1 } = params;
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.continuous(collection, { strategy, adjustment, cycle, rollOffset, rank }),
      queryFn: () =>
        getContinuousSeries(collection, {
          strategy,
          adjustment,
          cycle: cycle || undefined,
          rollOffset,
          rank,
        }),
      enabled: !!collection && (options.enabled ?? true),
      placeholderData: keepPreviousData,
      ...options,
    }),
  );
}

/**
 * A basket's composite weighted-sum series (Data-page exploration).
 *
 * ``basket`` is the discriminated wire descriptor — ``{kind:'saved',
 * basket_id}`` or ``{kind:'inline', asset_class, legs}``.  Disabled until a
 * basket is supplied.  ``placeholderData: keepPreviousData`` so changing the
 * date window keeps the previous curve on screen while the new one loads.
 */
export function useBasketSeries(basket, params = {}, options = {}) {
  const { start, end, field = 'close' } = params;
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.basketSeries(basket, { start, end, field }),
      queryFn: ({ signal }) => getBasketSeries(basket, { start, end, field, signal }),
      enabled: !!basket && (options.enabled ?? true),
      placeholderData: keepPreviousData,
      ...options,
    }),
  );
}

/** Available roll cycles for a futures collection. */
export function useAvailableCycles(collection, options = {}) {
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.cycles(collection),
      queryFn: () => getAvailableCycles(collection),
      enabled: !!collection && (options.enabled ?? true),
      ...options,
    }),
  );
}

/** Option-root catalogue. Returns the raw ``{ roots: [...] }`` payload. */
export function useOptionRoots(options = {}) {
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.optionRoots(),
      queryFn: ({ signal }) => getOptionRoots({ signal }),
      ...options,
    }),
  );
}

/** Distinct expirations for an option root. Disabled when root is falsy. */
export function useOptionExpirationsQuery(root, options = {}) {
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.optionExpirations(root),
      queryFn: () => getOptionExpirations(root),
      enabled: !!root && (options.enabled ?? true),
      ...options,
    }),
  );
}

/**
 * Per-contract option time-series. Disabled until both collection and
 * contractId are present (mirrors the old ``Promise.resolve(null)`` guard).
 */
export function useOptionContract(
  collection,
  contractId,
  { computeMissing = true, dateFrom = null, dateTo = null, ...options } = {},
) {
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.optionContract(collection, contractId, {
        computeMissing,
        dateFrom,
        dateTo,
      }),
      queryFn: () => getOptionContract(collection, contractId, { computeMissing, dateFrom, dateTo }),
      enabled: !!collection && !!contractId && (options.enabled ?? true),
      ...options,
    }),
  );
}

/**
 * Single-date smile snapshot for one expiration.
 *
 * ``placeholderData: keepPreviousData`` so toggling field (IV/Delta) keeps the
 * prior smile visible while the new one loads.
 */
export function useChainSnapshot(root, params = {}, options = {}) {
  const { date, type = 'C', expiration, field = 'iv', expiration_cycle = null } = params;
  return asAsyncResult(
    useQuery({
      queryKey: queryKeys.market.chainSnapshot(root, { date, type, expiration, field, expiration_cycle }),
      queryFn: () =>
        getChainSnapshot(root, {
          date,
          type,
          expirations: [expiration],
          field,
          ...(expiration_cycle ? { expiration_cycle } : {}),
        }),
      enabled: !!root && !!date && !!expiration && (options.enabled ?? true),
      placeholderData: keepPreviousData,
      ...options,
    }),
  );
}
