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
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

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

describe('<PortfolioPage> TradeLog integration', () => {
  it('mounts TradeLog below Statistics and renders trade rows with Holding column', () => {
    const trade = {
      input_id: 'SPY',
      entry_block_id: 'e1',
      entry_block_name: 'Entry',
      exit_block_id: 'x1',
      exit_block_name: 'Exit',
      open_bar: 0,
      close_bar: 2,
      direction: 'long',
      signed_weight: 0.5,
      holding_id: 'SigA',
      holding_name: 'SigA',
    };
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        legs: [{
          id: 1,
          label: 'SigA',
          type: 'signal',
          signalId: 's1',
          signalName: 'SigA',
          signalSpec: {
            rules: {
              entries: [{ id: 'e1', description: 'RSI<30' }],
              exits: [{ id: 'x1', description: 'RSI>70' }],
            },
          },
          weight: 100,
        }],
        results: resultsFixture({
          trades: [trade],
          positions: [{
            input_id: 'SPY',
            price: { label: 'close', values: [100, 102, 110] },
          }],
        }),
      }),
    );

    render(<PortfolioPage />);

    // TradeLog header is present
    const toggle = screen.getByTestId('trade-log-toggle');
    expect(toggle).toBeDefined();
    expect(screen.getByTestId('trade-log-count').textContent).toBe('(1)');

    // DOM order: Statistics title before returns-grid, returns-grid before trade-log
    const statsTitle = screen.getByText('Statistics');
    const tradeLog = screen.getByTestId('trade-log');
    const grid = screen.getByTestId('returns-grid');
    expect(statsTitle.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(grid.compareDocumentPosition(tradeLog) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    // Expand and verify the Holding column shows the leg label, plus the
    // ISO→ms timestamp conversion renders the open date correctly.
    fireEvent.click(toggle);
    expect(screen.getByTestId('holding-col-header').textContent).toBe('Holding');
    expect(screen.getByTestId('trade-holding').textContent).toBe('SigA');
    const row = screen.getByTestId('trade-row');
    // Open cell = 2024-01-02 (from results.dates[0])
    expect(row.querySelectorAll('td')[0].textContent).toContain('2024-01-02');
  });

  it('renders TradeLog with empty trades array without crashing', () => {
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        legs: [{ id: 1, label: 'SPY', weight: 100 }],
        results: resultsFixture({ trades: [], positions: [] }),
      }),
    );
    render(<PortfolioPage />);
    expect(screen.getByTestId('trade-log-count').textContent).toBe('(0)');
    // Expand and verify the empty state message.
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    expect(screen.getByTestId('trade-log-empty').textContent).toBe('No trades');
  });

  it('ISO→ms timestamp conversion: dates string array becomes parseable timestamps in TradeLog', () => {
    const trade = {
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'E',
      exit_block_id: null,
      exit_block_name: null,
      open_bar: 0,
      close_bar: null,
      direction: 'long',
      signed_weight: 1.0,
      holding_id: 'L', holding_name: 'L',
    };
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        legs: [{ id: 1, label: 'L', type: 'signal', signalSpec: { rules: { entries: [], exits: [] } }, weight: 100 }],
        results: resultsFixture({
          dates: ['2025-01-15', '2025-01-16'],
          portfolio_equity: [100.0, 101.0],
          trades: [trade],
          positions: [{ input_id: 'X', price: { label: 'close', values: [50, 51] } }],
        }),
      }),
    );
    render(<PortfolioPage />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    const row = screen.getByTestId('trade-row');
    // First column = open timestamp formatted YYYY-MM-DD; verifies that
    // new Date('2025-01-15').getTime() round-trips correctly into TradeLog.
    expect(row.querySelectorAll('td')[0].textContent).toContain('2025-01-15');
  });

  it('degrades to empty descriptions when a signal leg has no signalSpec', () => {
    const trade = {
      input_id: 'X',
      entry_block_id: 'e1',
      entry_block_name: 'EntryName',
      exit_block_id: 'x1',
      exit_block_name: 'ExitName',
      open_bar: 0,
      close_bar: 1,
      direction: 'long',
      signed_weight: 0.5,
      holding_id: 'L', holding_name: 'L',
    };
    vi.mocked(usePortfolio).mockReturnValue(
      baseHook({
        legs: [{ id: 1, label: 'L', type: 'signal', weight: 100 /* no signalSpec */ }],
        results: resultsFixture({
          trades: [trade],
          positions: [{ input_id: 'X', price: { label: 'close', values: [100, 105, 110] } }],
        }),
      }),
    );
    render(<PortfolioPage />);
    fireEvent.click(screen.getByTestId('trade-log-toggle'));
    // Entry/exit reason fall back to the block name; tooltip is empty.
    const entry = screen.getByTestId('trade-entry-reason');
    const exit = screen.getByTestId('trade-exit-reason');
    expect(entry.textContent).toBe('EntryName');
    expect(entry.getAttribute('data-reason-tooltip')).toBe('');
    expect(exit.textContent).toBe('ExitName');
    expect(exit.getAttribute('data-reason-tooltip')).toBe('');
  });
});
