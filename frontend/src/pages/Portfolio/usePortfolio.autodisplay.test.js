// @vitest-environment jsdom
//
// Auto-display (read-only cache) additions to usePortfolio:
//   - when the active config resolves to a CACHED key, its result is displayed
//     automatically (no Compute click, no compute call) via /cache/get;
//   - a cache MISS leaves the display blank (never computes);
//   - an edit that changes the key BLANKS the display immediately;
//   - a fresh Compute is never clobbered by a late auto-display get.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import usePortfolio from './usePortfolio';

const HIT_RESULT = { portfolio_equity: [100, 110], dates: ['2020-01-01', '2020-12-31'], from_cache: true };

// A controllable promise so a test can decide exactly when an in-flight
// getPortfolioCachedResult call resolves (simulates a slow network round-trip).
function createDeferred() {
  let resolve;
  const promise = new Promise((res) => { resolve = res; });
  return { promise, resolve };
}

vi.mock('../../api/portfolio', () => ({
  computePortfolio: vi.fn(() => Promise.resolve({ portfolio_equity: [1, 2], dates: ['a', 'b'], from_cache: false })),
  getPortfolioCachedResult: vi.fn(() => Promise.resolve({ result: null, from_cache: false })),
}));
vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
  getContinuousSeries: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
}));
vi.mock('../../api/options', () => ({
  getOptionCoverage: vi.fn(() => Promise.resolve({ root: 'X', start: '2005-12-01', end: '2025-06-30' })),
}));
vi.mock('../../api/persistence', () => ({ getPortfolio: vi.fn() }));
vi.mock('../Signals/requestBuilder', () => ({
  buildComputeRequestBody: vi.fn(() => ({ body: {}, missing: [] })),
}));
vi.mock('../Signals/hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([])),
}));
vi.mock('../../components/SaveControls', () => ({ useAutosave: vi.fn(), default: () => null }));

import { computePortfolio, getPortfolioCachedResult } from '../../api/portfolio';

const INSTR_LEG = { type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100, label: 'SPX' };
// A cached OPTION premium leg whose sizing/hold fields (nav_times, sizing_mode,
// futures_reference, hold_between_rolls) enter the compute body / backend key.
const OPT_LEG = {
  type: 'option_stream',
  collection: 'OPT_SP_500',
  option_type: 'put',
  cycle: 'M',
  maturity: { kind: 'end_of_month' },
  selection: { by_delta: 10 },
  stream: 'mid',
  weight: 100,
  label: 'SPX Put',
  hold_between_rolls: true,
  nav_times: 1.0,
};

beforeEach(() => {
  const store = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  });
  vi.clearAllMocks();
  getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });
});

describe('usePortfolio — auto-display cached result', () => {
  it('HIT: auto-displays the cached result with no compute call', async () => {
    getPortfolioCachedResult.mockResolvedValue({ result: HIT_RESULT, from_cache: true });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });

    await waitFor(() => expect(result.current.results).toEqual(HIT_RESULT));
    expect(getPortfolioCachedResult).toHaveBeenCalled();
    expect(computePortfolio).not.toHaveBeenCalled(); // auto-display NEVER computes
  });

  it('MISS: leaves the display blank and never computes', async () => {
    getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });

    await waitFor(() => expect(getPortfolioCachedResult).toHaveBeenCalled());
    // Give any pending microtasks a chance; result must remain null.
    await act(async () => { await Promise.resolve(); });
    expect(result.current.results).toBeNull();
    expect(computePortfolio).not.toHaveBeenCalled();
  });

  it('EDIT: an edit that changes the key blanks the displayed result', async () => {
    getPortfolioCachedResult.mockResolvedValue({ result: HIT_RESULT, from_cache: true });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });
    await waitFor(() => expect(result.current.results).toEqual(HIT_RESULT));

    // The edited config is uncached → the next get is a miss.
    getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });
    act(() => { result.current.updateLeg(0, { weight: 50 }); });

    // Display blanks (immediately on the key change; stays blank on the miss).
    await waitFor(() => expect(result.current.results).toBeNull());
  });

  it('EDIT option nav_times: editing a body-affecting option field blanks the stale result and re-probes', async () => {
    getPortfolioCachedResult.mockResolvedValue({ result: HIT_RESULT, from_cache: true });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(OPT_LEG); });
    await waitFor(() => expect(result.current.results).toEqual(HIT_RESULT));
    const callsBefore = getPortfolioCachedResult.mock.calls.length;

    // nav_times enters the compute body (backend key) → editing it MUST blank the
    // stale auto-displayed result and re-probe. Pre-fix autoDisplaySig omitted
    // this field, so the chart stayed stale while the badge flipped (SC5/SC6).
    getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });
    act(() => { result.current.updateLeg(0, { nav_times: 2.0 }); });

    await waitFor(() => expect(result.current.results).toBeNull());
    await waitFor(() =>
      expect(getPortfolioCachedResult.mock.calls.length).toBeGreaterThan(callsBefore),
    );
  });

  it('EDIT option sizing_mode: editing sizing_mode blanks the stale result', async () => {
    getPortfolioCachedResult.mockResolvedValue({ result: HIT_RESULT, from_cache: true });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(OPT_LEG); });
    await waitFor(() => expect(result.current.results).toEqual(HIT_RESULT));

    getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });
    act(() => { result.current.updateLeg(0, { sizing_mode: 'futures_notional' }); });

    await waitFor(() => expect(result.current.results).toBeNull());
  });

  it('COMPUTE-RACE: an edit during a slow compute invalidates the stale compute (no wrong-config curve)', async () => {
    const X_RESULT = { portfolio_equity: [1, 1], dates: ['x0', 'x1'], from_cache: false };
    // The compute for config X is slow (deferred).
    const deferredCompute = createDeferred();
    computePortfolio.mockReturnValue(deferredCompute.promise);
    getPortfolioCachedResult.mockResolvedValue({ result: null, from_cache: false });

    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });
    await waitFor(() => expect(getPortfolioCachedResult).toHaveBeenCalled());

    // Kick off Compute for X; let it reach the (deferred) computePortfolio call.
    let calcPromise;
    await act(async () => {
      calcPromise = result.current.handleCalculate();
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    });
    expect(computePortfolio).toHaveBeenCalled();

    // Edit to config Y WHILE X's compute is still in flight (bumps the run id).
    await act(async () => { result.current.updateLeg(0, { weight: 42 }); });

    // Now X's stale compute resolves.
    deferredCompute.resolve(X_RESULT);
    await act(async () => {
      await calcPromise; await Promise.resolve(); await Promise.resolve();
    });

    // The stale X compute must NOT have painted its curve over edited config Y.
    expect(result.current.results).not.toEqual(X_RESULT);
  });

  it('cache-get error: a rejected getPortfolioCachedResult leaves results null and a later Compute still works', async () => {
    getPortfolioCachedResult.mockRejectedValue(new Error('cache backend down'));
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });

    await waitFor(() => expect(getPortfolioCachedResult).toHaveBeenCalled());
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    // The rejection is swallowed — the display stays blank, never surfaced.
    expect(result.current.results).toBeNull();

    // A subsequent Compute still succeeds (the cache glitch never breaks it).
    const FRESH = { portfolio_equity: [5, 6], dates: ['a', 'b'], from_cache: false };
    computePortfolio.mockResolvedValue(FRESH);
    await act(async () => { await result.current.handleCalculate(); });
    expect(result.current.results).toEqual(FRESH);
  });

  it('does not auto-display when caching is disabled', async () => {
    localStorage.setItem('tcg-portfolio-cache-enabled', 'false');
    getPortfolioCachedResult.mockResolvedValue({ result: HIT_RESULT, from_cache: true });
    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });

    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(getPortfolioCachedResult).not.toHaveBeenCalled();
    expect(result.current.results).toBeNull();
  });

  it('RACE: a fresh Compute is never clobbered by a late-arriving auto-display get', async () => {
    const FRESH_RESULT = { portfolio_equity: [200, 220], dates: ['2021-01-01', '2021-12-31'], from_cache: false };
    const STALE_HIT = { portfolio_equity: [999, 999], dates: ['1999-01-01', '1999-12-31'], from_cache: true };

    // The auto-display get for the pre-Compute config is SLOW (deferred): it
    // won't resolve until we explicitly tell it to, below.
    const deferredGet = createDeferred();
    getPortfolioCachedResult.mockReturnValue(deferredGet.promise);
    computePortfolio.mockResolvedValue(FRESH_RESULT);

    const { result } = renderHook(() => usePortfolio());
    act(() => { result.current.addLeg(INSTR_LEG); });

    // The auto-display effect has kicked off its (still-pending) cache-get.
    await waitFor(() => expect(getPortfolioCachedResult).toHaveBeenCalled());
    expect(result.current.results).toBeNull();

    // A fresh Compute runs and resolves FIRST, superseding the in-flight get
    // (autoDisplayRunRef is bumped in handleCalculate before any await).
    await act(async () => { await result.current.handleCalculate(); });
    expect(result.current.results).toEqual(FRESH_RESULT);

    // NOW let the stale, late-arriving auto-display get resolve as a HIT.
    deferredGet.resolve({ result: STALE_HIT, from_cache: true });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    // The fresh Compute's result must survive untouched — the stale get must
    // NOT clobber it.
    expect(result.current.results).toEqual(FRESH_RESULT);
  });
});
