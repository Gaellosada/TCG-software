// @vitest-environment jsdom
//
// M7 regression (Portfolio mirror): ``oneshotStatus`` must NOT
// permanently mask ``cloudStatus``. When the debounced cloud autosave
// transitions to ``'saving'`` (or ``'error'``), it must take precedence
// over any stale ``'saved'`` from a recent one-shot category change /
// archive / save-current.
//
// M8 regression (Portfolio mirror): error messages from the persistence
// layer must reach the SaveStatus component (as ``errorMessage`` prop /
// tooltip / inline subtext) — not be discarded by bare ``catch {}``.
//
// Mirrors ``SignalsPage.statusPrecedence.test.jsx`` against the
// Portfolio page.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import {
  render, screen, fireEvent, cleanup, act, waitFor,
} from '@testing-library/react';

let capturedOnSelect = null;
let capturedOnSaveCurrent = null;
let capturedOnChangeItemCat = null;
let capturedOnArchive = null;
let capturedUpdateLeg = null;

vi.mock('./PersistedPortfolioPanel', () => ({
  default: ({
    portfolios,
    onSelect,
    onSaveCurrent,
    onChangeItemCat,
    onArchive,
  }) => {
    capturedOnSelect = onSelect;
    capturedOnSaveCurrent = onSaveCurrent;
    capturedOnChangeItemCat = onChangeItemCat;
    capturedOnArchive = onArchive;
    return (
      <div data-testid="persisted-panel">
        <button
          data-testid="persist-portfolio-btn"
          type="button"
          onClick={onSaveCurrent}
        >
          + Save current
        </button>
        {portfolios.map((p) => (
          <div key={p.id}>
            <button
              data-testid={`select-${p.id}`}
              type="button"
              onClick={() => onSelect(p.id)}
            >
              {p.name}
            </button>
            <button
              data-testid={`cat-${p.id}`}
              type="button"
              onClick={() => onChangeItemCat(p.id, 'DEV')}
            >
              move
            </button>
          </div>
        ))}
      </div>
    );
  },
}));

vi.mock('./HoldingsList', () => ({
  default: ({ legs, onUpdateLeg }) => {
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
  id: 'ptf-prec-1',
  type: 'portfolio',
  name: 'Prec Portfolio',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  legs: [
    { label: 'L1', type: 'instrument', collection: 'spot_daily', symbol: 'SPY', weight: 60 },
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
  describePersistenceError: (err) => {
    if (!err) return 'Unknown error';
    const status = err.status;
    const msg = err.message || String(err);
    if (typeof status !== 'number') return msg;
    if (status === 409) return `Conflict (409): ${msg}`;
    if (status === 413) return `Payload too large (413): ${msg}`;
    if (status === 422) return `Validation error (422): ${msg}`;
    if (status >= 400 && status < 500) return `Client error (${status}): ${msg}`;
    if (status >= 500) return `Server error (${status}): ${msg}`;
    return msg;
  },
}));

import PortfolioPage from './PortfolioPage';

beforeEach(() => {
  capturedOnSelect = null;
  capturedOnSaveCurrent = null;
  capturedOnChangeItemCat = null;
  capturedOnArchive = null;
  capturedUpdateLeg = null;
  mockListPortfolios.mockReset();
  mockCreatePortfolio.mockReset();
  mockUpdatePortfolio.mockReset();
  mockArchivePortfolio.mockReset();
  mockListPortfolios.mockResolvedValue([PERSISTED_DOC]);
  mockUpdatePortfolio.mockResolvedValue({ ...PERSISTED_DOC });
  mockArchivePortfolio.mockResolvedValue(null);
  mockCreatePortfolio.mockResolvedValue({ ...PERSISTED_DOC, id: 'ptf-new' });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// M7 — precedence
// ---------------------------------------------------------------------------
describe('<PortfolioPage> M7 — cloudStatus saving wins over stale oneshot saved', () => {
  it('after a successful one-shot category change, debounced autosave saving must surface', async () => {
    vi.useFakeTimers();
    // updatePortfolio: first call (category change) resolves; second
    // call (cloud autosave PUT) hangs so 'saving' stays visible.
    let callCount = 0;
    mockUpdatePortfolio.mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) return Promise.resolve({ ...PERSISTED_DOC, category: 'DEV' });
      return new Promise(() => {}); // never resolves
    });
    try {
      await act(async () => { render(<PortfolioPage />); });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Load the portfolio so persistedId is set and SaveStatus renders.
      expect(screen.queryByTestId('select-ptf-prec-1')).not.toBeNull();
      await act(async () => {
        fireEvent.click(screen.getByTestId('select-ptf-prec-1'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Trigger a one-shot category change — resolves to 'saved'.
      expect(capturedOnChangeItemCat).not.toBeNull();
      await act(async () => {
        capturedOnChangeItemCat('ptf-prec-1', 'DEV');
        await vi.advanceTimersByTimeAsync(0);
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // oneshotStatus = 'saved' is now visible.
      let el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('saved');

      // Edit a leg — kicks off the debounced backend autosave.
      expect(capturedUpdateLeg).not.toBeNull();
      await act(async () => {
        capturedUpdateLeg(0, { label: 'EDITED' });
      });

      // Advance past the 3s debounce — autosave fires (second call: hangs).
      await act(async () => { await vi.advanceTimersByTimeAsync(3100); });

      // SaveStatus must now show 'saving' (cloud autosave in flight),
      // NOT the stale 'saved' from the one-shot category change.
      el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('saving');
    } finally {
      vi.useRealTimers();
    }
  });
});

// ---------------------------------------------------------------------------
// M8 — error messages reach SaveStatus
// ---------------------------------------------------------------------------
describe('<PortfolioPage> M8 — error message surfacing on SaveStatus', () => {
  const errorCases = [
    { status: 409, message: 'duplicate id', expected: 'Conflict (409): duplicate id' },
    { status: 422, message: 'invalid payload', expected: 'Validation error (422): invalid payload' },
    { status: 413, message: 'too big', expected: 'Payload too large (413): too big' },
    { status: 500, message: 'kaboom', expected: 'Server error (500): kaboom' },
  ];

  it.each(errorCases)('surfaces $expected to SaveStatus on createPortfolio $status failure', async ({ status, message, expected }) => {
    const origConsoleError = console.error;
    console.error = () => {}; // suppress expected error log
    try {
      const err = new Error(message);
      err.status = status;
      mockCreatePortfolio.mockRejectedValue(err);

      await act(async () => { render(<PortfolioPage />); });
      await act(async () => {});

      // Invoke the captured "+ Save current" callback directly. The
      // ``handlePersistSave`` path is the simplest entry point that
      // triggers a one-shot error without needing a loaded portfolio.
      expect(capturedOnSaveCurrent).not.toBeNull();
      await act(async () => { capturedOnSaveCurrent(); });

      await waitFor(() => {
        const el = screen.queryByTestId('save-status');
        expect(el).not.toBeNull();
        expect(el.dataset.status).toBe('error');
      });
      const el = screen.getByTestId('save-status');
      // The error detail must be exposed as data-error-message AND as
      // the title attribute (tooltip).
      expect(el.dataset.errorMessage).toBe(expected);
      expect(el.getAttribute('title')).toBe(expected);
      // Visible inline subtext also exposes the detail.
      const detail = screen.queryByTestId('save-status-error-detail');
      expect(detail).not.toBeNull();
      expect(detail.textContent).toContain(expected);
    } finally {
      console.error = origConsoleError;
    }
  });
});
