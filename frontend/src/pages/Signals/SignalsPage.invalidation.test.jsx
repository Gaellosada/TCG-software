// @vitest-environment jsdom
//
// Proof that SignalsPage's invalidation is WIRED (not dead code): after a
// create/archive mutation, the signals list query refetches exactly once —
// and no unrelated persistence list (portfolios/indicators) refetches.
//
// This is the C3 "invalidation on user edits" guarantee verified at the PAGE
// level (the unit-level isolation of the invalidator lives in
// hooks/persistenceQueries.test.jsx).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, act, waitFor } from '@testing-library/react';

// Capture the SignalsList callbacks so the test can drive add/delete.
let capturedProps = {};
vi.mock('./SignalsList', () => ({
  default: (props) => {
    capturedProps = props;
    return <div data-testid="signals-list-stub" data-count={(props.signals || []).length} />;
  },
}));
vi.mock('./BlockEditor', () => ({ default: () => <div /> }));
vi.mock('./ParamsPanel', () => ({ default: () => <div /> }));
vi.mock('./InputsPanel', () => ({ default: () => <div /> }));
vi.mock('./ResultsView', () => ({ default: () => <div /> }));
vi.mock('../../components/Statistics', () => ({ default: () => <div /> }));
vi.mock('../../components/TradeLog', () => ({ default: () => <div /> }));
vi.mock('../../api/statistics', () => ({ fetchStatistics: vi.fn() }));
vi.mock('./hydrateIndicators', () => ({
  hydrateAvailableIndicators: () => Promise.resolve([]),
}));
vi.mock('../../api/signals', () => ({
  computeSignal: vi.fn(),
  collectIndicatorIds: () => new Set(),
}));
vi.mock('./runGate', () => ({
  computeRunGate: () => ({ runDisabledReason: null, missingIds: [] }),
}));

// Persistence API — count calls per resource to prove refetch + isolation.
const mockListSignals = vi.fn(() => Promise.resolve([]));
const mockListPortfolios = vi.fn(() => Promise.resolve([]));
const mockListIndicators = vi.fn(() => Promise.resolve([]));
const mockCreateSignal = vi.fn(() => Promise.resolve({}));
const mockArchiveSignal = vi.fn(() => Promise.resolve(null));
vi.mock('../../api/persistence', () => ({
  listSignals: (...a) => mockListSignals(...a),
  listPortfolios: (...a) => mockListPortfolios(...a),
  listIndicators: (...a) => mockListIndicators(...a),
  createSignal: (...a) => mockCreateSignal(...a),
  updateSignal: vi.fn(() => Promise.resolve({})),
  archiveSignal: (...a) => mockArchiveSignal(...a),
  setSignalLocked: vi.fn(() => Promise.resolve({})),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: () => false,
}));

import SignalsPage from './SignalsPage';

beforeEach(() => {
  capturedProps = {};
  mockListSignals.mockReset().mockResolvedValue([]);
  mockListPortfolios.mockReset().mockResolvedValue([]);
  mockListIndicators.mockReset().mockResolvedValue([]);
  mockCreateSignal.mockReset().mockResolvedValue({});
  mockArchiveSignal.mockReset().mockResolvedValue(null);
});
afterEach(cleanup);

describe('SignalsPage — invalidation is wired (C3)', () => {
  it('refetches the signals list exactly once after a create, and touches no other resource', async () => {
    render(<SignalsPage />);

    // Initial mount triggers the first list load.
    await waitFor(() => expect(mockListSignals).toHaveBeenCalledTimes(1));
    expect(capturedProps.onAdd).toBeTypeOf('function');

    const callsBefore = mockListSignals.mock.calls.length;

    // Trigger a create via the captured SignalsList onAdd.
    await act(async () => {
      await capturedProps.onAdd();
    });

    // createSignal fired, then the signals query was invalidated → refetch.
    expect(mockCreateSignal).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(mockListSignals.mock.calls.length).toBe(callsBefore + 1));

    // Isolation: no portfolios / indicators list was refetched by the signal edit.
    expect(mockListPortfolios).not.toHaveBeenCalled();
    expect(mockListIndicators).not.toHaveBeenCalled();
  });

  it('refetches the signals list exactly once after an archive', async () => {
    // Seed one signal so there is something to archive.
    mockListSignals.mockResolvedValue([
      { id: 'sig-a', name: 'Signal A', category: 'RESEARCH', inputs: [], rules: { entries: [], exits: [] }, settings: {}, description: '' },
    ]);

    render(<SignalsPage />);
    await waitFor(() => expect(mockListSignals).toHaveBeenCalledTimes(1));
    expect(capturedProps.onDelete).toBeTypeOf('function');

    const callsBefore = mockListSignals.mock.calls.length;

    // onDelete opens a confirm dialog; confirm it.
    await act(async () => {
      capturedProps.onDelete('sig-a');
    });
    const confirmBtn = await screen.findByRole('button', { name: /delete|confirm|archive/i });
    await act(async () => {
      confirmBtn.click();
    });

    expect(mockArchiveSignal).toHaveBeenCalledWith('sig-a');
    await waitFor(() => expect(mockListSignals.mock.calls.length).toBe(callsBefore + 1));
    expect(mockListPortfolios).not.toHaveBeenCalled();
    expect(mockListIndicators).not.toHaveBeenCalled();
  });
});
