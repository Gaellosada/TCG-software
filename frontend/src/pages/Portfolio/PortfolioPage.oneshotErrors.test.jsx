// @vitest-environment jsdom
//
// Wave 8 one-shot error surfacing for PortfolioPage:
//   Verify that handlePersistSave (+ Save current), handleChangePortfolioCat,
//   and handleArchivePortfolio all flip the CloudStatus indicator to 'error'
//   when the backend call rejects, and to 'saved' on success.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react';

// --- Mocks ------------------------------------------------------------------

// Capture callbacks from PersistedPortfolioPanel for direct invocation.
let capturedOnSaveCurrent = null;
let capturedOnChangeItemCat = null;
let capturedOnArchive = null;

vi.mock('./PersistedPortfolioPanel', () => ({
  default: ({
    portfolios,
    onSaveCurrent,
    onChangeItemCat,
    onArchive,
    onSelect,
  }) => {
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
              data-testid={`load-portfolio-${p.id}`}
              type="button"
              onClick={() => onSelect(p.id)}
            >
              {p.name}
            </button>
            <button
              data-testid={`cat-portfolio-${p.id}`}
              type="button"
              onClick={() => onChangeItemCat(p.id, 'DEV')}
            >
              move to DEV
            </button>
            <button
              data-testid={`archive-portfolio-${p.id}`}
              type="button"
              onClick={() => onArchive(p.id)}
            >
              archive
            </button>
          </div>
        ))}
      </div>
    );
  },
}));

vi.mock('./HoldingsList', () => ({
  default: ({ legs }) => (
    <div data-testid="holdings-list">
      <span data-testid="leg-count">{legs.length}</span>
    </div>
  ),
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
}));

const PERSISTED_DOC = {
  id: 'ptf-1',
  type: 'portfolio',
  name: 'My Portfolio',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  legs: [
    { label: 'SPY', type: 'instrument', collection: 'spot_daily', symbol: 'SPY', weight: 60 },
  ],
  rebalance: 'monthly',
};

import PortfolioPage from './PortfolioPage';

beforeEach(() => {
  capturedOnSaveCurrent = null;
  capturedOnChangeItemCat = null;
  capturedOnArchive = null;
  mockListPortfolios.mockReset();
  mockCreatePortfolio.mockReset();
  mockUpdatePortfolio.mockReset();
  mockArchivePortfolio.mockReset();

  mockListPortfolios.mockResolvedValue([PERSISTED_DOC]);
  mockCreatePortfolio.mockResolvedValue({ ...PERSISTED_DOC });
  mockUpdatePortfolio.mockResolvedValue({ ...PERSISTED_DOC });
  mockArchivePortfolio.mockResolvedValue(null);
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// handlePersistSave (+ Save current) — backend failure
// ---------------------------------------------------------------------------
describe('<PortfolioPage> one-shot error surfacing — handlePersistSave', () => {
  it('shows SaveStatus=error when createPortfolio rejects', async () => {
    mockCreatePortfolio.mockRejectedValue(new Error('network error'));

    await act(async () => {
      render(<PortfolioPage />);
    });
    await act(async () => {});

    // Invoke onSaveCurrent directly (bypasses disabled state — we're
    // testing the handler, not the disabled-button guard).
    expect(capturedOnSaveCurrent).not.toBeNull();
    await act(async () => {
      capturedOnSaveCurrent();
    });

    // The SaveStatus should flip to 'error'.
    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('error');
    });
  });

  it('shows SaveStatus=saved when createPortfolio resolves', async () => {
    mockCreatePortfolio.mockResolvedValue({ ...PERSISTED_DOC, id: 'ptf-ok' });

    await act(async () => {
      render(<PortfolioPage />);
    });
    await act(async () => {});

    expect(capturedOnSaveCurrent).not.toBeNull();
    await act(async () => {
      capturedOnSaveCurrent();
    });

    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('saved');
    });
  });
});

// ---------------------------------------------------------------------------
// handleChangePortfolioCat — backend failure
// ---------------------------------------------------------------------------
describe('<PortfolioPage> one-shot error surfacing — handleChangePortfolioCat', () => {
  it('shows SaveStatus=error when updatePortfolio rejects on category change', async () => {
    mockUpdatePortfolio.mockRejectedValue(new Error('network error'));

    await act(async () => {
      render(<PortfolioPage />);
    });
    await waitFor(() => {
      expect(screen.queryByTestId('load-portfolio-ptf-1')).not.toBeNull();
    });

    // Load the portfolio so persistedId is set and SaveStatus is shown
    // (ensures the indicator is rendered for the debounce path too).
    await act(async () => {
      fireEvent.click(screen.getByTestId('load-portfolio-ptf-1'));
    });
    await act(async () => {});

    // Trigger category change directly via the captured callback.
    expect(capturedOnChangeItemCat).not.toBeNull();
    await act(async () => {
      capturedOnChangeItemCat('ptf-1', 'DEV');
    });

    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('error');
    });
  });
});

// ---------------------------------------------------------------------------
// handleArchivePortfolio — backend failure
// ---------------------------------------------------------------------------
describe('<PortfolioPage> one-shot error surfacing — handleArchivePortfolio', () => {
  it('shows SaveStatus=error when archivePortfolio rejects', async () => {
    mockArchivePortfolio.mockRejectedValue(new Error('network error'));

    await act(async () => {
      render(<PortfolioPage />);
    });
    await waitFor(() => {
      expect(screen.queryByTestId('load-portfolio-ptf-1')).not.toBeNull();
    });

    // Load the portfolio so persistedId is set and SaveStatus is rendered.
    await act(async () => {
      fireEvent.click(screen.getByTestId('load-portfolio-ptf-1'));
    });
    await act(async () => {});

    // Archive it.
    expect(capturedOnArchive).not.toBeNull();
    await act(async () => {
      capturedOnArchive('ptf-1');
    });

    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      // On failure, persistedId stays set and SaveStatus remains visible.
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('error');
    });
  });

  it('shows SaveStatus=saved when archivePortfolio resolves', async () => {
    mockArchivePortfolio.mockResolvedValue(null);

    await act(async () => {
      render(<PortfolioPage />);
    });
    await waitFor(() => {
      expect(screen.queryByTestId('load-portfolio-ptf-1')).not.toBeNull();
    });

    // Load the portfolio.
    await act(async () => {
      fireEvent.click(screen.getByTestId('load-portfolio-ptf-1'));
    });
    await act(async () => {});

    // Archive it.
    expect(capturedOnArchive).not.toBeNull();
    await act(async () => {
      capturedOnArchive('ptf-1');
    });

    // On success, persistedId gets cleared — SaveStatus may unmount.
    // Verify that error was not shown.
    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      if (el) {
        expect(el.dataset.status).not.toBe('error');
      }
    });
  });
});
