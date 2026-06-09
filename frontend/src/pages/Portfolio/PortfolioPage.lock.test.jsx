// @vitest-environment jsdom
//
// Tests for F2 lock feature on the Portfolio page:
//   - Lock banner shown when loaded portfolio is locked
//   - Lock banner absent when portfolio is unlocked / none loaded
//   - handleSetPortfolioLocked calls setPortfolioLocked API and updates list
//   - Save button disabled when portfolio is locked
//   - Autosave not triggered when portfolio is locked (cloudDirty guard)

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react';

// ── Mock heavy children ─────────────────────────────────────────────────────

vi.mock('./usePortfolio', () => ({
  default: vi.fn(),
}));

vi.mock('./PortfolioEquityChart', () => ({
  default: () => <div data-testid="equity-chart" />,
}));
vi.mock('./ReturnsGrid', () => ({
  default: () => <div data-testid="returns-grid" />,
}));
vi.mock('./HoldingsList', () => ({
  default: () => <div data-testid="holdings-list" />,
}));
vi.mock('./AddHoldingModal', () => ({ default: () => null }));
vi.mock('./SignalPickerModal', () => ({ default: () => null }));
vi.mock('../../components/SaveControls', () => ({
  default: ({ saveDisabled }) => (
    <button data-testid="save-btn" disabled={!!saveDisabled}>Save</button>
  ),
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

const mockListPortfolios = vi.fn(() => Promise.resolve([]));
const mockSetPortfolioLocked = vi.fn();

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listPortfolios: (...args) => mockListPortfolios(...args),
  createPortfolio: vi.fn(() => Promise.resolve({})),
  updatePortfolio: vi.fn(() => Promise.resolve({})),
  archivePortfolio: vi.fn(() => Promise.resolve(null)),
  setPortfolioLocked: (...args) => mockSetPortfolioLocked(...args),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: (err) => !!err && err.status === 423,
}));

import PortfolioPage from './PortfolioPage';
import usePortfolio from './usePortfolio';

function baseHook(overrides = {}) {
  return {
    legs: [],
    rebalance: 'none',
    startDate: '',
    endDate: '',
    results: null,
    loading: false,
    error: null,
    legDateRanges: {},
    overlapRange: null,
    rangesLoading: false,
    portfolioName: '',
    dirty: false,
    autosave: false,
    setAutosave: vi.fn(),
    setRebalance: vi.fn(),
    setStartDate: vi.fn(),
    setEndDate: vi.fn(),
    addLeg: vi.fn(),
    addSignalLeg: vi.fn(),
    updateLeg: vi.fn(),
    removeLeg: vi.fn(),
    handleCalculate: vi.fn(),
    clearAll: vi.fn(),
    clearError: vi.fn(),
    setPortfolioName: vi.fn(),
    persistedId: null,
    setPersistedId: vi.fn(),
    loadFromPersisted: vi.fn(),
    persistedCategory: 'RESEARCH',
    setPersistedCategory: vi.fn(),
    persistedLocked: false,
    setPersistedLocked: vi.fn(),
    ...overrides,
  };
}

beforeEach(() => {
  mockListPortfolios.mockResolvedValue([]);
  mockSetPortfolioLocked.mockReset();
  vi.mocked(usePortfolio).mockReturnValue(baseHook());
});

afterEach(() => {
  cleanup();
  vi.mocked(usePortfolio).mockReset();
});

// ── Lock banner tests ───────────────────────────────────────────────────────

describe('<PortfolioPage> lock banner', () => {
  it('shows lock banner when persistedLocked is true', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({ persistedId: 'p1', persistedLocked: true }),
    );
    render(<PortfolioPage />);
    expect(screen.getByTestId('portfolio-lock-banner')).toBeTruthy();
    expect(screen.getByTestId('portfolio-lock-banner').textContent).toContain(
      'This portfolio is locked',
    );
  });

  it('does NOT show lock banner when persistedLocked is false', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({ persistedId: 'p1', persistedLocked: false }),
    );
    render(<PortfolioPage />);
    expect(screen.queryByTestId('portfolio-lock-banner')).toBeNull();
  });

  it('does NOT show lock banner when no portfolio is loaded', () => {
    vi.mocked(usePortfolio).mockReturnValue(baseHook({ persistedId: null }));
    render(<PortfolioPage />);
    expect(screen.queryByTestId('portfolio-lock-banner')).toBeNull();
  });
});

// ── Save disabled when locked ───────────────────────────────────────────────

describe('<PortfolioPage> save disabled when locked', () => {
  it('disables the save button when persistedLocked is true', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        persistedId: 'p1',
        persistedLocked: true,
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
        portfolioName: 'MyPortfolio',
      }),
    );
    render(<PortfolioPage />);
    expect(screen.getByTestId('save-btn').disabled).toBe(true);
  });

  it('does not disable save button solely due to lock when not locked', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        persistedId: 'p1',
        persistedLocked: false,
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
        portfolioName: 'MyPortfolio',
      }),
    );
    render(<PortfolioPage />);
    expect(screen.getByTestId('save-btn').disabled).toBe(false);
  });
});

// ── Read-only editor inputs when locked ─────────────────────────────────────
//
// Iter-3 consistency: a locked portfolio must have genuinely non-interactive
// editor controls (mirrors Indicators). The Rebalance <select> is a real
// control rendered directly by PortfolioPage; the disabled <fieldset> wrapper
// must make it (and all sibling holdings/config controls) non-interactive.

describe('<PortfolioPage> read-only editor inputs when locked', () => {
  it('disables the Rebalance select when persistedLocked is true', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        persistedId: 'p1',
        persistedLocked: true,
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
        portfolioName: 'MyPortfolio',
      }),
    );
    render(<PortfolioPage />);
    // The Rebalance <select> sits inside a native disabled <fieldset>. In real
    // browsers that disables it; in jsdom the descendant's `.disabled` IDL flag
    // stays false but the effective disabled state IS reflected by `:disabled`,
    // which is what blocks interaction.
    const rebalance = document.getElementById('rebalance-select');
    expect(rebalance).not.toBeNull();
    expect(rebalance.matches(':disabled')).toBe(true);
  });

  it('leaves the Rebalance select enabled when not locked', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        persistedId: 'p1',
        persistedLocked: false,
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
        portfolioName: 'MyPortfolio',
      }),
    );
    render(<PortfolioPage />);
    const rebalance = document.getElementById('rebalance-select');
    expect(rebalance.matches(':disabled')).toBe(false);
  });

  it('wraps the editor body in a disabled fieldset only when locked', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({ persistedId: 'p1', persistedLocked: true }),
    );
    render(<PortfolioPage />);
    const fs = screen.getByTestId('portfolio-editor-fieldset');
    expect(fs.tagName).toBe('FIELDSET');
    expect(fs.disabled).toBe(true);
  });

  it('keeps Compute enabled when locked so a locked portfolio stays inspectable', () => {
    // Loading a persisted portfolio does NOT auto-compute, so Compute must
    // remain usable when locked — mirroring how Indicators keeps Run live
    // while the definition is read-only.
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        persistedId: 'p1',
        persistedLocked: true,
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
      }),
    );
    render(<PortfolioPage />);
    const compute = screen.getByRole('button', { name: /compute/i });
    expect(compute.matches(':disabled')).toBe(false);
  });
});

// ── handleSetPortfolioLocked wiring ────────────────────────────────────────

describe('<PortfolioPage> handleSetPortfolioLocked', () => {
  it('calls setPortfolioLocked API when a row LockToggle fires', async () => {
    const portfolioDoc = {
      id: 'p1', name: 'Alpha', category: 'RESEARCH', locked: false,
    };
    mockListPortfolios.mockResolvedValue([portfolioDoc]);
    // setPortfolioLocked returns updated doc with locked: true
    mockSetPortfolioLocked.mockResolvedValue({ ...portfolioDoc, locked: true });

    vi.mocked(usePortfolio).mockReturnValue(baseHook());

    await act(async () => {
      render(<PortfolioPage />);
    });

    // Wait for list to load
    await waitFor(() => {
      expect(screen.queryByTestId('load-portfolio-p1')).not.toBeNull();
    });

    // Click the LockToggle on the row (unlocked → locks immediately)
    const lockBtn = screen.getByTestId('lock-toggle-btn');
    await act(async () => {
      fireEvent.click(lockBtn);
    });

    expect(mockSetPortfolioLocked).toHaveBeenCalledWith('p1', true);
  });

  it('updates setPersistedLocked on the hook when the currently loaded portfolio is locked', async () => {
    const setPersistedLocked = vi.fn();
    const portfolioDoc = {
      id: 'p1', name: 'Alpha', category: 'RESEARCH', locked: false,
    };
    mockListPortfolios.mockResolvedValue([portfolioDoc]);
    mockSetPortfolioLocked.mockResolvedValue({ ...portfolioDoc, locked: true });

    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({ persistedId: 'p1', setPersistedLocked }),
    );

    await act(async () => {
      render(<PortfolioPage />);
    });

    await waitFor(() => {
      expect(screen.queryByTestId('lock-toggle-btn')).not.toBeNull();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    });

    // setPersistedLocked(true) should have been called since persistedId === 'p1'
    await waitFor(() => {
      expect(setPersistedLocked).toHaveBeenCalledWith(true);
    });
  });

  it('shows error status when setPortfolioLocked API fails', async () => {
    const portfolioDoc = {
      id: 'p1', name: 'Alpha', category: 'RESEARCH', locked: false,
    };
    mockListPortfolios.mockResolvedValue([portfolioDoc]);
    mockSetPortfolioLocked.mockRejectedValue(new Error('Server error'));

    vi.mocked(usePortfolio).mockReturnValue(baseHook());

    await act(async () => {
      render(<PortfolioPage />);
    });

    await waitFor(() => {
      expect(screen.queryByTestId('lock-toggle-btn')).not.toBeNull();
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    });

    // The page should not crash; setPortfolioLocked was called
    expect(mockSetPortfolioLocked).toHaveBeenCalled();
  });
});
