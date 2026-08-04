// @vitest-environment jsdom
//
// Behavioural proof for the stale-while-revalidate layer. These tests assert
// the four properties the migration promises, using the REAL query hooks with
// a mocked api client and a SHARED QueryClient across mounts (so the cache
// behaves exactly as it does across route navigation in the running app).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act, renderHook } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { makeTestClient } from '../test/queryWrapper';

// Mock the api client modules the hooks call.
vi.mock('../api/data', () => ({
  listCollections: vi.fn(),
  listInstruments: vi.fn(),
  getInstrumentPrices: vi.fn(),
  getContinuousSeries: vi.fn(),
  getAvailableCycles: vi.fn(),
}));
vi.mock('../api/options', () => ({
  getOptionRoots: vi.fn(),
  getOptionExpirations: vi.fn(),
  getOptionContract: vi.fn(),
  getChainSnapshot: vi.fn(),
}));
vi.mock('../api/dataV2', () => ({
  listObjectsV2: vi.fn(),
  getObjectDetailV2: vi.fn(),
  getObjectFacetsV2: vi.fn(),
  getObjectSeriesV2: vi.fn(),
  getSeriesV2: vi.fn(),
  getContinuousFuturesV2: vi.fn(),
  getV2FuturesCycles: vi.fn(),
  getContinuousOptionsV2: vi.fn(),
}));

import { getInstrumentPrices } from '../api/data';
import { getObjectFacetsV2, getObjectSeriesV2 } from '../api/dataV2';
import { useInstrumentPrices, useObjectFacetsV2, useObjectSeriesV2 } from './marketQueries';

const PRICES_A = { dates: [20240101, 20240102], close: [10, 11] };

// A tiny component that renders the prices hook's state so we can observe the
// loading flash (or absence of it) and the rendered data.
function PriceProbe({ collection = 'INDEX', instrument = 'IND_SP_500' }) {
  const { data, loading } = useInstrumentPrices(collection, instrument);
  return (
    <div>
      <span data-testid="state">{loading ? 'LOADING' : 'READY'}</span>
      <span data-testid="close">{data ? data.close.join(',') : 'none'}</span>
    </div>
  );
}

function renderProbe(client, props = {}) {
  return render(
    <QueryClientProvider client={client}>
      <PriceProbe {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('SWR: no spinner on re-navigation', () => {
  it('(a) second mount renders cached data with NO loading state', async () => {
    getInstrumentPrices.mockResolvedValue(PRICES_A);
    // ONE client shared across both mounts == surviving the cache across nav.
    const client = makeTestClient();

    // First mount: no cache yet → a single LOADING frame, then READY.
    const first = renderProbe(client);
    expect(first.getByTestId('state').textContent).toBe('LOADING');
    await waitFor(() => expect(first.getByTestId('state').textContent).toBe('READY'));
    expect(first.getByTestId('close').textContent).toBe('10,11');
    first.unmount(); // navigate away

    // Second mount (navigate back): the cache is warm. The VERY FIRST render
    // must already be READY with data — no loading flash.
    const second = renderProbe(client);
    expect(second.getByTestId('state').textContent).toBe('READY');
    expect(second.getByTestId('close').textContent).toBe('10,11');
  });

  it('first-ever mount (cold cache) does show the loading state once', async () => {
    // Documents the accepted exception: only the first load spins.
    getInstrumentPrices.mockResolvedValue(PRICES_A);
    const client = makeTestClient();
    const { getByTestId } = renderProbe(client);
    expect(getByTestId('state').textContent).toBe('LOADING');
    await waitFor(() => expect(getByTestId('state').textContent).toBe('READY'));
  });
});

describe('SWR: silent background revalidation', () => {
  it('(b) a stale entry refetches on remount and patches the view without a loading flash', async () => {
    // staleTime 0 → the entry is immediately stale, so remount triggers a
    // background refetch. The cached value renders instantly (no LOADING),
    // then the new value replaces it silently.
    getInstrumentPrices.mockResolvedValueOnce(PRICES_A);
    const client = makeTestClient();
    client.setDefaultOptions({ queries: { retry: false, gcTime: Infinity, staleTime: 0 } });

    const first = renderProbe(client);
    await waitFor(() => expect(first.getByTestId('close').textContent).toBe('10,11'));
    first.unmount();

    // Backend now returns a DIFFERENT payload (a new bar appended).
    const PRICES_B = { dates: [20240101, 20240102, 20240103], close: [10, 11, 12] };
    getInstrumentPrices.mockResolvedValueOnce(PRICES_B);

    const second = renderProbe(client);
    // Instant: shows the stale cached value with NO loading flash.
    expect(second.getByTestId('state').textContent).toBe('READY');
    expect(second.getByTestId('close').textContent).toBe('10,11');
    // Background refetch lands and silently patches to the new value.
    await waitFor(() => expect(second.getByTestId('close').textContent).toBe('10,11,12'));
    // Never flashed a loading state during the revalidate.
    expect(second.getByTestId('state').textContent).toBe('READY');
    expect(getInstrumentPrices).toHaveBeenCalledTimes(2);
  });
});

describe('SWR: structural sharing (diff-and-patch)', () => {
  it('(c) unchanged rows keep referential identity across a refetch', async () => {
    // TanStack structural sharing: if a refetch returns deep-equal data, the
    // identical object references are preserved, so memoised consumers do not
    // re-render. We assert identity at the array/element level.
    const rows1 = { items: [{ symbol: 'AAA' }, { symbol: 'BBB' }], total: 2 };
    // A structurally-equal-but-fresh object graph from the "server".
    const rows2 = { items: [{ symbol: 'AAA' }, { symbol: 'BBB' }], total: 2 };

    const client = makeTestClient();
    client.setDefaultOptions({ queries: { retry: false, gcTime: Infinity, staleTime: 0 } });
    const { listInstruments } = await import('../api/data');
    listInstruments.mockResolvedValueOnce(rows1);

    const { useInstruments } = await import('./marketQueries');
    const wrapper = ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useInstruments('FUT_SP_500'), { wrapper });

    await waitFor(() => expect(result.current.data).toBeTruthy());
    const firstData = result.current.data;
    const firstItems = firstData.items;
    const firstRowAAA = firstData.items[0];

    // Trigger a background refetch returning a fresh-but-equal graph.
    listInstruments.mockResolvedValueOnce(rows2);
    await act(async () => {
      await result.current.refetch();
    });

    // Structural sharing keeps the SAME references for unchanged data.
    expect(result.current.data).toBe(firstData);
    expect(result.current.data.items).toBe(firstItems);
    expect(result.current.data.items[0]).toBe(firstRowAAA);
  });

  it('changed rows DO get new references while untouched rows keep identity', async () => {
    const rows1 = { items: [{ symbol: 'AAA', last: 10 }, { symbol: 'BBB', last: 20 }], total: 2 };
    // BBB changed; AAA identical.
    const rows2 = { items: [{ symbol: 'AAA', last: 10 }, { symbol: 'BBB', last: 99 }], total: 2 };

    const client = makeTestClient();
    client.setDefaultOptions({ queries: { retry: false, gcTime: Infinity, staleTime: 0 } });
    const { listInstruments } = await import('../api/data');
    listInstruments.mockResolvedValueOnce(rows1);

    const { useInstruments } = await import('./marketQueries');
    const wrapper = ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useInstruments('FUT_SP_500'), { wrapper });
    await waitFor(() => expect(result.current.data).toBeTruthy());
    const firstAAA = result.current.data.items[0];
    const firstBBB = result.current.data.items[1];

    listInstruments.mockResolvedValueOnce(rows2);
    await act(async () => {
      await result.current.refetch();
    });
    // Wait for the re-render carrying the patched value before asserting.
    await waitFor(() => expect(result.current.data.items[1].last).toBe(99));

    // Untouched AAA keeps identity; changed BBB is a new reference; value updated.
    expect(result.current.data.items[0]).toBe(firstAAA);
    expect(result.current.data.items[1]).not.toBe(firstBBB);
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Database v2 — the filtered/paginated series drill-down.
//
// The property that matters here is the ENABLED GATE on useObjectSeriesV2:
// while ``filters`` is null the query must not fire. That gate is the whole
// point of the feature — the unfiltered predecessor shipped 38 MB, so a hook
// that requests before a filter exists defeats it.
//
// A bare "was not called" assertion is not enough: it also passes if the hook
// is broken outright, or if the module mock is mis-wired. Every gate test below
// therefore ends by lifting the gate and asserting the call DOES happen — the
// negative and the positive share one code path.
// ───────────────────────────────────────────────────────────────────────────

const PAGE_1 = { items: [{ serie_id: 1 }], total: 2, skip: 0, limit: 1 };
const PAGE_2 = { items: [{ serie_id: 2 }], total: 2, skip: 1, limit: 1 };

function v2Wrapper(client) {
  return ({ children }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe('useObjectSeriesV2: the enabled gate', () => {
  it('does NOT fetch while filters is null, and DOES once filters exist', async () => {
    getObjectSeriesV2.mockResolvedValue(PAGE_1);
    const client = makeTestClient();
    const { result, rerender } = renderHook(
      ({ filters }) => useObjectSeriesV2(12, filters),
      { wrapper: v2Wrapper(client), initialProps: { filters: null } },
    );

    // Flush every microtask/effect a real fetch would have needed.
    await act(async () => { await Promise.resolve(); });
    expect(getObjectSeriesV2).not.toHaveBeenCalled();
    // A disabled query must look settled-and-empty, not stuck spinning.
    expect(result.current.loading).toBe(false);
    expect(result.current.data).toBeNull();

    // Lift the gate — proves the mock and the hook are actually wired, so the
    // "not called" assertion above cannot pass for the wrong reason.
    rerender({ filters: { limit: 1 } });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);
  });

  it('does NOT fetch while filters is undefined (hook called with one arg)', async () => {
    getObjectSeriesV2.mockResolvedValue(PAGE_1);
    const client = makeTestClient();
    const { result, rerender } = renderHook(
      ({ filters }) => useObjectSeriesV2(12, filters),
      { wrapper: v2Wrapper(client), initialProps: { filters: undefined } },
    );
    await act(async () => { await Promise.resolve(); });
    expect(getObjectSeriesV2).not.toHaveBeenCalled();

    rerender({ filters: {} });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);
  });

  it('an EMPTY filter object is "applied" and does fetch (only null gates)', async () => {
    // The filter panel's initial "apply with nothing set" state is ``{}``: a
    // real, bounded request (backend defaults + limit 50), not a gated one.
    getObjectSeriesV2.mockResolvedValue(PAGE_1);
    const client = makeTestClient();
    const { result } = renderHook(() => useObjectSeriesV2(12, {}), {
      wrapper: v2Wrapper(client),
    });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);
  });

  it('does NOT fetch without an objectId, even with filters applied', async () => {
    getObjectSeriesV2.mockResolvedValue(PAGE_1);
    const client = makeTestClient();
    const { result, rerender } = renderHook(
      ({ id }) => useObjectSeriesV2(id, { limit: 1 }),
      { wrapper: v2Wrapper(client), initialProps: { id: null } },
    );
    await act(async () => { await Promise.resolve(); });
    expect(getObjectSeriesV2).not.toHaveBeenCalled();

    rerender({ id: 12 });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);
  });

  it('an explicit enabled:true canNOT re-open the gate while filters is null', async () => {
    // Regression guard for a real defect found while mutation-testing this
    // task: with ``enabled`` written BEFORE ``...options`` in the useQuery
    // config, a caller's ``enabled: true`` replaced the whole expression and
    // the hook fetched with filters === null — exactly the unbounded request
    // the endpoint exists to prevent. ``options.enabled`` must only ever
    // NARROW the gate.
    getObjectSeriesV2.mockResolvedValue(PAGE_1);
    const client = makeTestClient();
    const { result, rerender } = renderHook(
      ({ filters }) => useObjectSeriesV2(12, filters, { enabled: true }),
      { wrapper: v2Wrapper(client), initialProps: { filters: null } },
    );
    await act(async () => { await Promise.resolve(); });
    expect(getObjectSeriesV2).not.toHaveBeenCalled();

    rerender({ filters: { limit: 1 } });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);
  });

  it('an explicit enabled:true canNOT re-open the gate while objectId is null', async () => {
    getObjectSeriesV2.mockResolvedValue(PAGE_1);
    const client = makeTestClient();
    const { result, rerender } = renderHook(
      ({ id }) => useObjectSeriesV2(id, { limit: 1 }, { enabled: true }),
      { wrapper: v2Wrapper(client), initialProps: { id: null } },
    );
    await act(async () => { await Promise.resolve(); });
    expect(getObjectSeriesV2).not.toHaveBeenCalled();

    rerender({ id: 12 });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);
  });

  it('honours an explicit enabled:false even with an id and filters', async () => {
    getObjectSeriesV2.mockResolvedValue(PAGE_1);
    const client = makeTestClient();
    const { result, rerender } = renderHook(
      ({ enabled }) => useObjectSeriesV2(12, { limit: 1 }, { enabled }),
      { wrapper: v2Wrapper(client), initialProps: { enabled: false } },
    );
    await act(async () => { await Promise.resolve(); });
    expect(getObjectSeriesV2).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);
  });
});

describe('useObjectSeriesV2: filters and paging', () => {
  it('forwards the filter object to the api client and adds an AbortSignal', async () => {
    getObjectSeriesV2.mockResolvedValue(PAGE_1);
    const client = makeTestClient();
    const filters = { optionType: 'put', serieType: 'bbba', freq: '1m', skip: 50, limit: 50 };
    const { result } = renderHook(() => useObjectSeriesV2(12, filters), {
      wrapper: v2Wrapper(client),
    });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));

    const [objectId, arg] = getObjectSeriesV2.mock.calls[0];
    expect(objectId).toBe(12);
    // Every filter must arrive verbatim (v2 domains: call|put|both,
    // bar|value|greeks|bbba|any, 1m|daily|any) plus the signal, nothing else.
    expect(arg).toEqual({ ...filters, signal: expect.any(AbortSignal) });
  });

  it('keys the cache by filter VALUE: a different page refetches, an equal one does not', async () => {
    getObjectSeriesV2.mockResolvedValueOnce(PAGE_1).mockResolvedValueOnce(PAGE_2);
    const client = makeTestClient();
    const { result, rerender } = renderHook(
      ({ filters }) => useObjectSeriesV2(12, filters),
      { wrapper: v2Wrapper(client), initialProps: { filters: { skip: 0, limit: 1 } } },
    );
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);

    // Same filters, brand-new object identity → same key hash → cache hit, so
    // NO second request. (Fails if the key omitted filters only by accident.)
    rerender({ filters: { skip: 0, limit: 1 } });
    await act(async () => { await Promise.resolve(); });
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);

    // ...and the same filters in a DIFFERENT key order are still one entry.
    // TanStack's hashKey sorts object keys, so this holds — but only while the
    // key builder puts the raw object in the key. Serialising it there (e.g.
    // JSON.stringify(filters)) would make property order significant and split
    // the cache on a meaningless difference; this is what catches that.
    rerender({ filters: { limit: 1, skip: 0 } });
    await act(async () => { await Promise.resolve(); });
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(1);

    // Next page → different key → a real second request with the new page.
    rerender({ filters: { skip: 1, limit: 1 } });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_2));
    expect(getObjectSeriesV2).toHaveBeenCalledTimes(2);
    expect(getObjectSeriesV2.mock.calls[1][1]).toMatchObject({ skip: 1, limit: 1 });
  });

  it('keepPreviousData: the current page stays visible while the next one loads', async () => {
    let releasePage2;
    getObjectSeriesV2
      .mockResolvedValueOnce(PAGE_1)
      .mockImplementationOnce(() => new Promise((resolve) => { releasePage2 = resolve; }));
    const client = makeTestClient();
    const { result, rerender } = renderHook(
      ({ filters }) => useObjectSeriesV2(12, filters),
      { wrapper: v2Wrapper(client), initialProps: { filters: { skip: 0, limit: 1 } } },
    );
    await waitFor(() => expect(result.current.data).toEqual(PAGE_1));

    // Page 2 in flight: page 1 must still be on screen with no loading state
    // (that is what placeholderData: keepPreviousData buys).
    rerender({ filters: { skip: 1, limit: 1 } });
    await act(async () => { await Promise.resolve(); });
    expect(result.current.data).toEqual(PAGE_1);
    expect(result.current.loading).toBe(false);

    await act(async () => { releasePage2(PAGE_2); });
    await waitFor(() => expect(result.current.data).toEqual(PAGE_2));
  });
});

describe('useObjectFacetsV2', () => {
  const FACETS = {
    object_id: 12,
    kind: 'option',
    expirations: [{ expiration: '2026-03-20', contracts: 118 }],
    option_types: ['call', 'put'],
    serie_types: [{ type: 'bbba', freq: '1m', series: 236 }],
    totals: { contracts: 118, series: 236 },
  };

  it('does NOT fetch without an objectId, and DOES once one arrives', async () => {
    getObjectFacetsV2.mockResolvedValue(FACETS);
    const client = makeTestClient();
    const { result, rerender } = renderHook(({ id }) => useObjectFacetsV2(id), {
      wrapper: v2Wrapper(client),
      initialProps: { id: null },
    });
    await act(async () => { await Promise.resolve(); });
    expect(getObjectFacetsV2).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(false);

    rerender({ id: 12 });
    await waitFor(() => expect(result.current.data).toEqual(FACETS));
    expect(getObjectFacetsV2).toHaveBeenCalledTimes(1);
    expect(getObjectFacetsV2.mock.calls[0][0]).toBe(12);
    expect(getObjectFacetsV2.mock.calls[0][1]).toEqual({ signal: expect.any(AbortSignal) });
  });

  it('an explicit enabled:true canNOT re-open the gate while objectId is null', async () => {
    getObjectFacetsV2.mockResolvedValue(FACETS);
    const client = makeTestClient();
    const { result, rerender } = renderHook(
      ({ id }) => useObjectFacetsV2(id, { enabled: true }),
      { wrapper: v2Wrapper(client), initialProps: { id: null } },
    );
    await act(async () => { await Promise.resolve(); });
    expect(getObjectFacetsV2).not.toHaveBeenCalled();

    rerender({ id: 12 });
    await waitFor(() => expect(result.current.data).toEqual(FACETS));
    expect(getObjectFacetsV2).toHaveBeenCalledTimes(1);
  });

  it('is cached per objectId — a warm remount shows facets with no loading flash', async () => {
    getObjectFacetsV2.mockResolvedValue(FACETS);
    const client = makeTestClient();
    const first = renderHook(() => useObjectFacetsV2(12), { wrapper: v2Wrapper(client) });
    expect(first.result.current.loading).toBe(true); // cold: spins once
    await waitFor(() => expect(first.result.current.data).toEqual(FACETS));
    first.unmount();

    // Warm remount: facets are there on the VERY FIRST render, so the filter
    // panel never blanks out. (The default staleTime of 0 still fires a silent
    // background revalidate — that is the SWR contract asserted above, not a
    // cache miss; what matters is that it does not surface as ``loading``.)
    const second = renderHook(() => useObjectFacetsV2(12), { wrapper: v2Wrapper(client) });
    expect(second.result.current.data).toEqual(FACETS);
    expect(second.result.current.loading).toBe(false);
  });

  it('refetches when the objectId changes (facets are per object)', async () => {
    const OTHER = { ...FACETS, object_id: 13, totals: { contracts: 4, series: 8 } };
    getObjectFacetsV2.mockResolvedValueOnce(FACETS).mockResolvedValueOnce(OTHER);
    const client = makeTestClient();
    const { result, rerender } = renderHook(({ id }) => useObjectFacetsV2(id), {
      wrapper: v2Wrapper(client),
      initialProps: { id: 12 },
    });
    await waitFor(() => expect(result.current.data).toEqual(FACETS));

    rerender({ id: 13 });
    await waitFor(() => expect(result.current.data).toEqual(OTHER));
    expect(getObjectFacetsV2.mock.calls.map((c) => c[0])).toEqual([12, 13]);
  });
});
