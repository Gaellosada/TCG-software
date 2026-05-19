// @vitest-environment jsdom
//
// B2 regression (Portfolio mirror): re-clicking the same already-selected
// persisted portfolio must NOT overwrite in-progress edits with the stale
// backend snapshot. Mirrors ``SignalsPage.reselectGuard.test.jsx``.
//
// Before the fix, ``handleSelectPersisted`` unconditionally re-hydrated
// from the backend doc, silently discarding edits typed since the last
// debounced save. The guard checks ``cloudDirtyRef`` and ``persistedId``
// to suppress the re-hydrate when re-clicking the loaded portfolio
// while it still has unsaved local edits.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import {
  render, screen, fireEvent, cleanup, act,
} from '@testing-library/react';

// --- Mocks --------------------------------------------------------------

// Capture callbacks from PersistedPortfolioPanel for direct invocation.
let capturedOnSelect = null;

vi.mock('./PersistedPortfolioPanel', () => ({
  default: ({ portfolios, onSelect }) => {
    capturedOnSelect = onSelect;
    return (
      <div data-testid="persisted-panel">
        {portfolios.map((p) => (
          <button
            key={p.id}
            data-testid={`select-${p.id}`}
            type="button"
            onClick={() => onSelect(p.id)}
          >
            {p.name}
          </button>
        ))}
      </div>
    );
  },
}));

// HoldingsList stub: exposes the legs list AND surface an
// ``onUpdateLeg(idx, updates)`` button so the test can simulate a
// local edit (changes ``portfolio.legs``) before the debounce fires.
let capturedLegs = null;
let capturedUpdateLeg = null;

vi.mock('./HoldingsList', () => ({
  default: ({ legs, onUpdateLeg }) => {
    capturedLegs = legs;
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
  id: 'ptf-reselect-1',
  type: 'portfolio',
  name: 'Reselect Portfolio',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  legs: [
    { label: 'ORIGINAL', type: 'instrument', collection: 'spot_daily', symbol: 'SPY', weight: 60 },
  ],
  rebalance: 'monthly',
};

const mockListPortfolios = vi.fn();
const mockCreatePortfolio = vi.fn();
const mockUpdatePortfolio = vi.fn();
const mockArchivePortfolio = vi.fn();

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listPortfolios: (...args) => mockListPortfolios(...args),
  createPortfolio: (...args) => mockCreatePortfolio(...args),
  updatePortfolio: (...args) => mockUpdatePortfolio(...args),
  archivePortfolio: (...args) => mockArchivePortfolio(...args),
  describePersistenceError: (err) => (err && err.message) || String(err),
}));

import PortfolioPage from './PortfolioPage';

beforeEach(() => {
  capturedOnSelect = null;
  capturedLegs = null;
  capturedUpdateLeg = null;
  mockListPortfolios.mockReset();
  mockCreatePortfolio.mockReset();
  mockUpdatePortfolio.mockReset();
  mockArchivePortfolio.mockReset();
  mockListPortfolios.mockResolvedValue([PERSISTED_DOC]);
  mockUpdatePortfolio.mockResolvedValue({ ...PERSISTED_DOC });
  mockCreatePortfolio.mockResolvedValue({ ...PERSISTED_DOC });
  mockArchivePortfolio.mockResolvedValue(null);
});

afterEach(() => {
  cleanup();
});

describe('<PortfolioPage> — re-select guard (B2 Portfolio mirror)', () => {
  it('re-clicking the same already-selected row preserves in-progress edits before the debounced save fires', async () => {
    vi.useFakeTimers();
    try {
      await act(async () => {
        render(<PortfolioPage />);
      });
      // Let the list fetch microtask settle.
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Select the persisted portfolio for the first time.
      await act(async () => {
        fireEvent.click(screen.getByTestId('select-ptf-reselect-1'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Verify hydrate happened — the single leg label is "ORIGINAL".
      expect(screen.getByTestId('leg-count').textContent).toBe('1');
      expect(screen.getByTestId('leg-0-label').textContent).toBe('ORIGINAL');
      expect(capturedUpdateLeg).not.toBeNull();

      // User mutates a leg locally — the debounce has NOT fired yet.
      await act(async () => {
        capturedUpdateLeg(0, { label: 'IN-PROGRESS EDIT' });
      });

      // Confirm the edit is reflected in the holdings-list stub.
      expect(screen.getByTestId('leg-0-label').textContent).toBe('IN-PROGRESS EDIT');

      // User clicks the SAME row again BEFORE the debounce has elapsed.
      // Without the guard, ``handleSelectPersisted`` would re-hydrate and
      // overwrite the local edit with the stale backend snapshot.
      await act(async () => {
        fireEvent.click(screen.getByTestId('select-ptf-reselect-1'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // The edit must still be present — guard suppressed the re-hydrate.
      expect(screen.getByTestId('leg-0-label').textContent).toBe('IN-PROGRESS EDIT');

      // updatePortfolio must NOT have been called yet (debounce 3s
      // hasn't fired, no time was advanced past it).
      expect(mockUpdatePortfolio).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('clicking a different row DOES re-hydrate (guard only suppresses same-id re-clicks)', async () => {
    // Two persisted portfolios; user loads one, edits, then clicks the
    // OTHER — the guard must NOT block hydration to the new id.
    const PERSISTED_DOC_2 = {
      ...PERSISTED_DOC,
      id: 'ptf-reselect-2',
      name: 'Other Portfolio',
      legs: [
        { label: 'OTHER', type: 'instrument', collection: 'spot_daily', symbol: 'AGG', weight: 40 },
      ],
    };
    mockListPortfolios.mockResolvedValue([PERSISTED_DOC, PERSISTED_DOC_2]);

    vi.useFakeTimers();
    try {
      await act(async () => {
        render(<PortfolioPage />);
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Load portfolio #1.
      await act(async () => {
        fireEvent.click(screen.getByTestId('select-ptf-reselect-1'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByTestId('leg-0-label').textContent).toBe('ORIGINAL');

      // Edit it.
      await act(async () => {
        capturedUpdateLeg(0, { label: 'EDITED-1' });
      });
      expect(screen.getByTestId('leg-0-label').textContent).toBe('EDITED-1');

      // Click portfolio #2 — different id. Guard must let the
      // re-hydrate through.
      await act(async () => {
        fireEvent.click(screen.getByTestId('select-ptf-reselect-2'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Editor now shows the OTHER portfolio's legs.
      expect(screen.getByTestId('leg-0-label').textContent).toBe('OTHER');
    } finally {
      vi.useRealTimers();
    }
  });
});
