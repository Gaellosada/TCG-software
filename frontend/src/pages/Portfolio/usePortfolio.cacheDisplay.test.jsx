// @vitest-environment jsdom
//
// Regression for the ≤275ms stale-result flash (extends FIX A). The auto-display
// effect only advanced the live-key mirror (currentKeyRef) INSIDE its 275ms
// debounced timer. So a compute dispatched for config A that LANDS within the
// debounce window after the user edits to config B still saw liveKey == A's key
// (baselined at compute start, timer not yet fired) → shouldDisplayComputeResult
// returned true → A's stale result was displayed for the modified config B.
//
// FIX: null currentKeyRef SYNCHRONOUSLY at the top of the auto-display effect
// (after the range gate) on every key-affecting edit. A compute landing before
// the debounce timer then sees liveKey=null !== computeKey(A) and is dropped.
//
// This drives a DEFERRED compute (resolved manually, with ~0ms elapsed and the
// debounce timer never firing) so it deterministically hits the sub-275ms
// window the fix targets — on the buggy code A would display; on the fix it is
// dropped and the modified config stays blank.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

// Cache must be ENABLED for the auto-display / live-key path to run.
vi.mock('../../lib/userSettings', () => ({
  isPortfolioCacheEnabled: vi.fn(() => true),
}));

// Distinct, config-dependent cache keys so A (weight 60) and B (weight 80) differ.
vi.mock('../../lib/computeCacheKey', () => ({
  computeCacheKey: vi.fn(async (body) => `key:${JSON.stringify(body?.weights || {})}`),
}));

// Local cache: nothing pre-seeded (getCached → null) so the only way A's result
// could appear is the auto-display DISPLAY decision on the landing compute.
vi.mock('../../lib/portfolioCache', () => ({
  getCached: vi.fn(async () => null),
  putCached: vi.fn(async () => {}),
}));

vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
  getContinuousSeries: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
}));

vi.mock('../../api/options', () => ({
  getOptionCoverage: vi.fn(() => Promise.resolve({ root: 'X', start: '2005-12-01', end: '2025-06-30' })),
}));

vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([])),
}));

vi.mock('../../components/SaveControls', () => ({
  useAutosave: vi.fn(),
  default: () => null,
}));

// computePortfolio is DEFERRED — the test resolves it by hand so the landing
// can be placed precisely inside the post-edit debounce window.
const A_RESULT = { equity: [1, 2], dates: ['A-ONLY-MARKER'] };
let resolveCompute;
vi.mock('../../api/portfolio', () => ({
  computePortfolio: vi.fn(() => new Promise((res) => { resolveCompute = res; })),
}));

import usePortfolio from './usePortfolio';
import { computePortfolio } from '../../api/portfolio';

function createStorageStub() {
  const store = new Map();
  return {
    getItem: vi.fn((k) => (store.has(k) ? store.get(k) : null)),
    setItem: vi.fn((k, v) => { store.set(k, String(v)); }),
    removeItem: vi.fn((k) => { store.delete(k); }),
    clear: vi.fn(() => { store.clear(); }),
  };
}

beforeEach(() => {
  vi.stubGlobal('localStorage', createStorageStub());
  vi.clearAllMocks();
  resolveCompute = undefined;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('usePortfolio — no stale-result flash when a compute lands inside the post-edit debounce window', () => {
  it('a compute for config A landing right after an edit to B does NOT display A', async () => {
    const { result } = renderHook(() => usePortfolio());

    // Config A: a single instrument leg, weight 60.
    act(() => {
      result.current.addLeg({
        label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 60,
      });
    });
    // Let the per-leg range effect settle so overlapRange resolves.
    await act(async () => { await new Promise((r) => setTimeout(r, 60)); });
    expect(result.current.results).toBeNull();

    // Dispatch the compute for A but hold it in flight (deferred). Flush enough
    // microtasks for handleCalculate to get past hydrate + computeCacheKey and
    // reach `await computePortfolio(...)`.
    let calcPromise;
    await act(async () => {
      calcPromise = result.current.handleCalculate();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(computePortfolio).toHaveBeenCalledTimes(1);
    expect(typeof resolveCompute).toBe('function'); // compute A is parked in flight

    // Edit to config B (weight 80) WHILE A is in flight. This re-runs the
    // auto-display effect, which (with the fix) synchronously nulls the live-key
    // mirror. The 275ms debounce timer is scheduled but has NOT fired.
    act(() => {
      result.current.updateLeg(0, { weight: 80 });
    });

    // Land compute A immediately (≈0ms elapsed, well inside the 275ms window).
    await act(async () => {
      resolveCompute(A_RESULT);
      await calcPromise;
      await Promise.resolve();
    });

    // FIX: A's result must have been DROPPED (liveKey was nulled by the edit),
    // so the modified config B stays blank. On the buggy code liveKey was still
    // A's key and A would have been displayed here.
    expect(result.current.results).toBeNull();
    expect(result.current.results).not.toBe(A_RESULT);
  });
});
