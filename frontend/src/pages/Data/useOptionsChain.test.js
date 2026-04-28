// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useOptionsChain } from './useOptionsChain';

// Mock the API client (C1.1 deliverable). We define the module shape here
// so tests run even before options.js is landed.
vi.mock('../../api/options', () => ({
  getOptionChain: vi.fn(),
  getOptionContract: vi.fn(),
}));

import { getOptionChain } from '../../api/options';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** ISO YYYY-MM-DD for today in local time (mirrors hook implementation). */
function todayISO() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

/** today + n days ISO. */
function addDays(isoDate, n) {
  const d = new Date(`${isoDate}T00:00:00`);
  d.setDate(d.getDate() + n);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

// ---------------------------------------------------------------------------
// localStorage stub — verify Decision C: zero writes during hook usage.
// ---------------------------------------------------------------------------
function createStorageStub() {
  const store = new Map();
  return {
    getItem: vi.fn((k) => (store.has(k) ? store.get(k) : null)),
    setItem: vi.fn((k, v) => { store.set(k, String(v)); }),
    removeItem: vi.fn((k) => { store.delete(k); }),
    clear: vi.fn(() => { store.clear(); }),
  };
}

let storageStub;

beforeEach(() => {
  storageStub = createStorageStub();
  vi.stubGlobal('localStorage', storageStub);
  vi.clearAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useOptionsChain — initial state', () => {
  it('exposes default filter values', () => {
    const { result } = renderHook(() => useOptionsChain());
    const { filters } = result.current;

    const today = todayISO();
    expect(filters.root).toBeNull();
    expect(filters.date).toBe(today);
    expect(filters.type).toBe('both');
    expect(filters.expirationMin).toBe(today);
    expect(filters.expirationMax).toBe(addDays(today, 90));
    expect(filters.strikeMin).toBeNull();
    expect(filters.strikeMax).toBeNull();
    expect(filters.computeMissing).toBe(false);
  });

  it('chainData is null before any fetch', () => {
    const { result } = renderHook(() => useOptionsChain());
    expect(result.current.chainData).toBeNull();
  });

  it('loading is false before any fetch', () => {
    const { result } = renderHook(() => useOptionsChain());
    expect(result.current.loading).toBe(false);
  });

  it('accepts an initialRoot argument', () => {
    const { result } = renderHook(() => useOptionsChain('OPT_SP_500'));
    expect(result.current.filters.root).toBe('OPT_SP_500');
  });
});

describe('useOptionsChain — fetchChain happy path', () => {
  it('populates chainData with the API response and clears loading', async () => {
    const mockResponse = { root: 'OPT_SP_500', rows: [{ contract_id: 'c1' }] };
    vi.mocked(getOptionChain).mockResolvedValueOnce(mockResponse);

    const { result } = renderHook(() => useOptionsChain('OPT_SP_500'));

    await act(async () => {
      await result.current.fetchChain();
    });

    expect(result.current.chainData).toEqual(mockResponse);
    expect(result.current.loading).toBe(false);
  });

  it('passes current filters (including root and computeMissing) to getOptionChain', async () => {
    vi.mocked(getOptionChain).mockResolvedValueOnce({ root: 'OPT_SP_500', rows: [] });

    const { result } = renderHook(() => useOptionsChain('OPT_SP_500'));

    await act(async () => {
      await result.current.fetchChain();
    });

    expect(getOptionChain).toHaveBeenCalledWith(
      'OPT_SP_500',
      expect.objectContaining({
        computeMissing: false,
        type: 'both',
      }),
    );
  });

  it('does nothing when root is null', async () => {
    const { result } = renderHook(() => useOptionsChain()); // no initialRoot

    await act(async () => {
      await result.current.fetchChain();
    });

    expect(getOptionChain).not.toHaveBeenCalled();
    expect(result.current.chainData).toBeNull();
  });
});

describe('useOptionsChain — updateFilters', () => {
  it('merges a partial update into filters', () => {
    const { result } = renderHook(() => useOptionsChain());

    act(() => {
      result.current.updateFilters({ root: 'OPT_GOLD' });
    });

    expect(result.current.filters.root).toBe('OPT_GOLD');
    // Other fields unchanged
    expect(result.current.filters.type).toBe('both');
    expect(result.current.filters.computeMissing).toBe(false);
  });

  it('uses the updated root when fetchChain is called afterwards', async () => {
    vi.mocked(getOptionChain).mockResolvedValueOnce({ root: 'OPT_GOLD', rows: [] });

    const { result } = renderHook(() => useOptionsChain());

    act(() => {
      result.current.updateFilters({ root: 'OPT_GOLD' });
    });

    await act(async () => {
      await result.current.fetchChain();
    });

    expect(getOptionChain).toHaveBeenCalledWith('OPT_GOLD', expect.any(Object));
  });

  it('updating multiple filter fields at once works', () => {
    const { result } = renderHook(() => useOptionsChain('OPT_SP_500'));

    act(() => {
      result.current.updateFilters({ type: 'C', strikeMin: 4000, strikeMax: 5000 });
    });

    expect(result.current.filters.type).toBe('C');
    expect(result.current.filters.strikeMin).toBe(4000);
    expect(result.current.filters.strikeMax).toBe(5000);
    expect(result.current.filters.root).toBe('OPT_SP_500'); // unchanged
  });
});

describe('useOptionsChain — computeMissing toggle is transient (Decision C)', () => {
  it('computeMissing defaults to false', () => {
    const { result } = renderHook(() => useOptionsChain());
    expect(result.current.filters.computeMissing).toBe(false);
  });

  it('can be toggled to true via updateFilters', () => {
    const { result } = renderHook(() => useOptionsChain());

    act(() => {
      result.current.updateFilters({ computeMissing: true });
    });

    expect(result.current.filters.computeMissing).toBe(true);
  });

  it('localStorage.setItem is never called — no persistence (Decision C)', async () => {
    vi.mocked(getOptionChain).mockResolvedValue({ root: 'OPT_SP_500', rows: [] });

    const { result } = renderHook(() => useOptionsChain('OPT_SP_500'));

    act(() => {
      result.current.updateFilters({ computeMissing: true });
    });

    await act(async () => {
      await result.current.fetchChain();
    });

    expect(storageStub.setItem).not.toHaveBeenCalled();
  });

  it('localStorage.getItem is never called — no read-back on init (Decision C)', () => {
    renderHook(() => useOptionsChain('OPT_SP_500'));
    expect(storageStub.getItem).not.toHaveBeenCalled();
  });
});

describe('useOptionsChain — AbortError handling', () => {
  it('swallows AbortError silently — chainData remains null', async () => {
    const abortErr = new DOMException('The user aborted a request.', 'AbortError');
    vi.mocked(getOptionChain).mockRejectedValueOnce(abortErr);

    const { result } = renderHook(() => useOptionsChain('OPT_SP_500'));

    await act(async () => {
      await result.current.fetchChain();
    });

    expect(result.current.chainData).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('swallows AbortError — chainData is not overwritten if already populated', async () => {
    const good = { root: 'OPT_SP_500', rows: [{ contract_id: 'c1' }] };
    vi.mocked(getOptionChain)
      .mockResolvedValueOnce(good) // first fetch succeeds
      .mockRejectedValueOnce(new DOMException('aborted', 'AbortError')); // second aborted

    const { result } = renderHook(() => useOptionsChain('OPT_SP_500'));

    await act(async () => { await result.current.fetchChain(); });
    expect(result.current.chainData).toEqual(good);

    await act(async () => { await result.current.fetchChain(); });
    // chainData must NOT be overwritten with null or an error
    expect(result.current.chainData).toEqual(good);
  });
});

describe('useOptionsChain — generic error surfaced', () => {
  it('sets chainData.error on non-abort failures', async () => {
    const networkErr = new Error('Network failure');
    vi.mocked(getOptionChain).mockRejectedValueOnce(networkErr);

    const { result } = renderHook(() => useOptionsChain('OPT_SP_500'));

    await act(async () => {
      await result.current.fetchChain();
    });

    expect(result.current.chainData).toEqual({ error: networkErr });
    expect(result.current.loading).toBe(false);
  });

  it('surfaces the original error object in chainData.error', async () => {
    const err = new TypeError('fetch failed');
    vi.mocked(getOptionChain).mockRejectedValueOnce(err);

    const { result } = renderHook(() => useOptionsChain('OPT_SP_500'));

    await act(async () => { await result.current.fetchChain(); });

    expect(result.current.chainData.error).toBe(err);
  });
});
