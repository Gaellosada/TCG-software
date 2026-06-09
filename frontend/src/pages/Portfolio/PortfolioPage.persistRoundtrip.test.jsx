// @vitest-environment jsdom
//
// Backend-persistence round-trip for the Portfolio page:
//
//   1. Mount PortfolioPage with a persisted portfolio in the backend
//      list (non-empty legs + rebalance set).
//   2. User clicks the saved-portfolio entry — assert the editor
//      receives the hydrated legs (regression for "legs don't persist").
//   3. User mutates a leg — within ~600ms, ``updatePortfolio`` is
//      called with the new payload including the edited leg.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react';

// --- Mock heavy children to keep the test unit-level ---------------------

let capturedHoldings = null;
let capturedUpdateLeg = null;

vi.mock('./HoldingsList', () => ({
  default: ({ legs, onUpdateLeg }) => {
    capturedHoldings = legs;
    capturedUpdateLeg = onUpdateLeg;
    return (
      <div data-testid="holdings-list">
        <span data-testid="leg-count">{legs.length}</span>
        {legs.map((l, i) => (
          <span key={l.id ?? i} data-testid={`leg-${i}-label`}>{l.label}</span>
        ))}
      </div>
    );
  },
}));

vi.mock('./PortfolioEquityChart', () => ({
  default: () => <div data-testid="equity-chart" />,
}));
vi.mock('./ReturnsGrid', () => ({
  default: () => <div data-testid="returns-grid" />,
}));
vi.mock('./AddHoldingModal', () => ({ default: () => null }));
vi.mock('./SignalPickerModal', () => ({ default: () => null }));
vi.mock('../../components/SaveControls', () => ({
  default: () => <div data-testid="save-controls" />,
  useAutosave: vi.fn(),
}));
vi.mock('../../components/TimeRangeSlider', () => ({
  default: () => <div data-testid="time-range" />,
}));
vi.mock('../../components/ConfirmDialog', () => ({ default: () => null }));
vi.mock('../../components/Statistics', () => ({
  default: () => <div data-testid="statistics" />,
}));
vi.mock('../../components/TradeLog', () => ({
  default: () => <div data-testid="trade-log" />,
}));
vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(() => Promise.resolve({ dates: [] })),
  getContinuousSeries: vi.fn(() => Promise.resolve({ dates: [] })),
}));
vi.mock('../../api/statistics', () => ({
  fetchStatistics: vi.fn(() => new Promise(() => {})),
}));
vi.mock('./signalLegRange', () => ({
  fetchSignalLegRange: vi.fn(() => Promise.resolve({ id: null, start: null, end: null })),
}));

const PERSISTED_DOC = {
  id: 'ptf-1',
  type: 'portfolio',
  name: 'My Saved Portfolio',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  legs: [
    { label: 'SPY', type: 'instrument', collection: 'spot_daily', symbol: 'SPY', weight: 60 },
    { label: 'AGG', type: 'instrument', collection: 'spot_daily', symbol: 'AGG', weight: 40 },
  ],
  rebalance: 'monthly',
};

const mockListPortfolios = vi.fn(() => Promise.resolve([PERSISTED_DOC]));
const mockUpdatePortfolio = vi.fn(() => Promise.resolve({ ...PERSISTED_DOC }));
const mockCreatePortfolio = vi.fn(() => Promise.resolve({ ...PERSISTED_DOC }));
const mockArchivePortfolio = vi.fn(() => Promise.resolve(null));

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listPortfolios: (...args) => mockListPortfolios(...args),
  createPortfolio: (...args) => mockCreatePortfolio(...args),
  updatePortfolio: (...args) => mockUpdatePortfolio(...args),
  archivePortfolio: (...args) => mockArchivePortfolio(...args),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: (err) => !!err && err.status === 423,
}));

import PortfolioPage from './PortfolioPage';

beforeEach(() => {
  capturedHoldings = null;
  capturedUpdateLeg = null;
  mockListPortfolios.mockClear();
  mockUpdatePortfolio.mockClear();
  mockCreatePortfolio.mockClear();
  mockListPortfolios.mockResolvedValue([PERSISTED_DOC]);
});

afterEach(() => {
  cleanup();
});

describe('<PortfolioPage> — backend hydrate + autosave', () => {
  it('hydrates legs and rebalance from the backend doc when a persisted portfolio is selected', async () => {
    await act(async () => {
      render(<PortfolioPage />);
    });

    // Wait for the persisted list to appear.
    await waitFor(() => {
      expect(screen.queryByTestId('load-portfolio-ptf-1')).not.toBeNull();
    });

    // Initially there are zero legs.
    expect(screen.getByTestId('leg-count').textContent).toBe('0');

    // Click the persisted portfolio to load it.
    await act(async () => {
      fireEvent.click(screen.getByTestId('load-portfolio-ptf-1'));
    });

    // The HoldingsList should now show 2 legs.
    await waitFor(() => {
      expect(screen.getByTestId('leg-count').textContent).toBe('2');
      expect(screen.getByTestId('leg-0-label').textContent).toBe('SPY');
      expect(screen.getByTestId('leg-1-label').textContent).toBe('AGG');
    });
  });

  it('PUTs the edited legs to the backend within ~3100ms', async () => {
    vi.useFakeTimers();
    try {
      await act(async () => {
        render(<PortfolioPage />);
      });
      // Drain the initial list-fetch microtask.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });

      // Load the persisted portfolio.
      await act(async () => {
        fireEvent.click(screen.getByTestId('load-portfolio-ptf-1'));
      });
      // Allow seeding effect to run.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });

      mockUpdatePortfolio.mockClear();
      expect(capturedUpdateLeg).not.toBeNull();

      // Edit leg 0 weight from 60 → 75.
      await act(async () => {
        capturedUpdateLeg(0, { weight: 75 });
      });

      // Within the debounce window the PUT must fire.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3100);
      });

      expect(mockUpdatePortfolio).toHaveBeenCalled();
      const [calledId, body] = mockUpdatePortfolio.mock.calls[0];
      expect(calledId).toBe('ptf-1');
      expect(body.legs).toHaveLength(2);
      expect(body.legs[0].weight).toBe(75);
      expect(body.legs[1].weight).toBe(40);
      expect(body.rebalance).toBe('monthly');
      expect(body.category).toBe('RESEARCH');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('<PortfolioPage> — 423 on autosave flips to read-only', () => {
  it('shows the lock banner when the debounced autosave is rejected with 423', async () => {
    vi.useFakeTimers();
    try {
      await act(async () => { render(<PortfolioPage />); });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Load the persisted (unlocked) portfolio — no banner yet.
      await act(async () => {
        fireEvent.click(screen.getByTestId('load-portfolio-ptf-1'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(screen.queryByTestId('portfolio-lock-banner')).toBeNull();

      // Next autosave rejects with HTTP 423 (locked elsewhere).
      const e = new Error('Document is locked');
      e.status = 423;
      mockUpdatePortfolio.mockRejectedValueOnce(e);

      // Edit a leg to make it dirty, then advance past the debounce.
      await act(async () => { capturedUpdateLeg(0, { weight: 75 }); });
      await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      expect(mockUpdatePortfolio).toHaveBeenCalled();
      // The 423 flips the LOCAL locked flag → read-only lock banner appears.
      expect(screen.getByTestId('portfolio-lock-banner')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a NON-locked autosave error (500) does NOT show the lock banner', async () => {
    vi.useFakeTimers();
    try {
      await act(async () => { render(<PortfolioPage />); });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      await act(async () => {
        fireEvent.click(screen.getByTestId('load-portfolio-ptf-1'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      const e = new Error('boom');
      e.status = 500;
      mockUpdatePortfolio.mockRejectedValueOnce(e);

      await act(async () => { capturedUpdateLeg(0, { weight: 75 }); });
      await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      expect(mockUpdatePortfolio).toHaveBeenCalled();
      // Generic error must NOT flip the editor to read-only.
      expect(screen.queryByTestId('portfolio-lock-banner')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
