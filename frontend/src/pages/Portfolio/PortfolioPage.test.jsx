// @vitest-environment jsdom
//
// PortfolioPage integration — verifies the Statistics panel is wired
// between the equity chart and the returns grid, and that it receives
// the right inputs in the right format.
//
// The page composes many heavy children (PortfolioEquityChart →
// Plotly, ReturnsGrid, etc.). We mock only what we need to assert on:
//   - usePortfolio so we can pump synthetic results into the page
//   - PortfolioEquityChart / ReturnsGrid so we can detect DOM order
//   - the statistics fetch so the Statistics panel doesn't hit the network

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

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

vi.mock('./AddHoldingModal', () => ({
  default: () => null,
}));

vi.mock('./SignalPickerModal', () => ({
  default: () => null,
}));

vi.mock('../../components/SaveControls', () => ({
  default: () => <div data-testid="save-controls" />,
  useAutosave: vi.fn(),
}));

vi.mock('../../components/TimeRangeSlider', () => ({
  default: () => <div data-testid="time-range" />,
}));

vi.mock('../../components/ConfirmDialog', () => ({
  default: () => null,
}));

vi.mock('../../api/statistics', () => ({
  fetchStatistics: vi.fn(() => new Promise(() => {})),
}));

import PortfolioPage from './PortfolioPage';
import usePortfolio from './usePortfolio';
import { fetchStatistics } from '../../api/statistics';

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
    savePortfolio: vi.fn(),
    loadPortfolio: vi.fn(),
    deleteSavedPortfolio: vi.fn(),
    getSavedPortfolios: vi.fn(() => []),
    ...overrides,
  };
}

function resultsFixture(overrides = {}) {
  return {
    dates: ['2024-01-02', '2024-01-03', '2024-01-04'],
    portfolio_equity: [100.0, 100.5, 101.2],
    leg_equities: {},
    raw_leg_equities: {},
    rebalance_dates: [],
    monthly_returns: { headers: [], rows: [] },
    yearly_returns: { headers: [], rows: [] },
    date_range: { start: '2024-01-02', end: '2024-01-04' },
    rebalance: 'none',
    return_type: 'log',
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(usePortfolio).mockReturnValue(baseHook());
});

afterEach(() => {
  cleanup();
  vi.mocked(fetchStatistics).mockClear();
  vi.mocked(usePortfolio).mockReset();
});

describe('<PortfolioPage> Statistics integration', () => {
  it('does NOT render Statistics when there are no results', () => {
    vi.mocked(usePortfolio).mockReturnValue(baseHook({ results: null }));
    render(<PortfolioPage />);
    expect(screen.queryByText('Statistics')).toBeNull();
    expect(fetchStatistics).not.toHaveBeenCalled();
  });

  it('renders Statistics between PortfolioEquityChart and ReturnsGrid when results are present', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
        results: resultsFixture(),
      }),
    );
    const { container } = render(<PortfolioPage />);

    // Statistics title is rendered by the component
    const statsTitle = screen.getByText('Statistics');
    expect(statsTitle).toBeDefined();

    // DOM order: equity-chart appears before statistics-title which
    // appears before returns-grid.
    const chart = screen.getByTestId('equity-chart');
    const grid = screen.getByTestId('returns-grid');
    // node.compareDocumentPosition returns a bitfield; bit 4 = "follows".
    expect(chart.compareDocumentPosition(statsTitle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(statsTitle.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    // Helper to silence lint about unused container — also doubles as a
    // sanity check that the page actually rendered something.
    expect(container.firstChild).not.toBeNull();
  });

  it('passes YYYYMMDD-integer dates and the portfolio equity curve to the Statistics fetch', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
        results: resultsFixture(),
      }),
    );
    render(<PortfolioPage />);
    expect(fetchStatistics).toHaveBeenCalled();
    const args = vi.mocked(fetchStatistics).mock.calls[0][0];
    expect(args.dates).toEqual([20240102, 20240103, 20240104]);
    expect(args.equity).toEqual([100.0, 100.5, 101.2]);
    expect(args.riskFreeRate).toBeCloseTo(0.04, 6);
  });

  it('does not crash with a single-point equity curve (skips Statistics)', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
        results: resultsFixture({
          dates: ['2024-01-02'],
          portfolio_equity: [100.0],
        }),
      }),
    );
    render(<PortfolioPage />);
    // Statistics requires length ≥ 2 — should silently skip.
    expect(screen.queryByText('Statistics')).toBeNull();
    expect(fetchStatistics).not.toHaveBeenCalled();
  });

  it('skips Statistics gracefully when dates are malformed', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
        results: resultsFixture({
          dates: ['bogus', 'also-bogus', 'still-bogus'],
        }),
      }),
    );
    render(<PortfolioPage />);
    expect(screen.queryByText('Statistics')).toBeNull();
    expect(fetchStatistics).not.toHaveBeenCalled();
  });
});
