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

import { getInstrumentPrices } from '../api/data';
import { useInstrumentPrices } from './marketQueries';

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
