// @vitest-environment jsdom
//
// TC4.10: SignalsPage passes defaultRiskFreeRate from localStorage to <Statistics>.
// Separate file to isolate the Statistics prop-capture mock.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';

// Capture the defaultRiskFreeRate prop passed to Statistics on mount.
let capturedDefaultRfr = undefined;
let capturedOnRun = null;
let capturedOnSelect = null;

vi.mock('./SignalsList', () => ({
  default: ({ onSelect }) => {
    capturedOnSelect = onSelect;
    return <div data-testid="signals-list-stub" />;
  },
}));
vi.mock('./BlockEditor', () => ({
  default: () => <div data-testid="block-editor-stub" />,
}));
vi.mock('./ParamsPanel', () => ({
  default: ({ onRun }) => {
    capturedOnRun = onRun;
    return <div data-testid="params-panel-stub" />;
  },
}));
vi.mock('./InputsPanel', () => ({
  default: () => <div data-testid="inputs-panel-stub" />,
}));
vi.mock('./ResultsView', () => ({
  default: () => <div data-testid="results-view-stub" />,
}));
// Statistics mock that captures the defaultRiskFreeRate prop.
vi.mock('../../components/Statistics', () => ({
  default: ({ defaultRiskFreeRate }) => {
    capturedDefaultRfr = defaultRiskFreeRate;
    return <div data-testid="statistics-stub" />;
  },
}));
vi.mock('../../components/TradeLog', () => ({
  default: () => <div data-testid="trade-log-stub" />,
}));
vi.mock('../../api/statistics', () => ({
  fetchStatistics: vi.fn(),
}));
vi.mock('./hydrateIndicators', () => ({
  hydrateAvailableIndicators: () => [],
}));

const mockComputeSignal = vi.fn();
vi.mock('../../api/signals', () => ({
  computeSignal: (...args) => mockComputeSignal(...args),
  collectIndicatorIds: () => new Set(),
}));
vi.mock('./runGate', () => ({
  computeRunGate: () => ({ runDisabledReason: null, missingIds: [] }),
}));
vi.mock('./requestBuilder', async () => {
  const actual = await vi.importActual('./requestBuilder');
  return {
    ...actual,
    buildComputeRequestBody: () => ({
      body: { spec: {}, indicators: [] },
      missing: [],
    }),
  };
});

// Provide one signal so SignalsPage auto-selects it (enabling handleRun).
const SIG_A = {
  id: 'sig-rfr-a', name: 'RFR Test Signal',
  inputs: [],
  rules: { entries: [], exits: [] },
  settings: { dont_repeat: true },
  doc: '',
};

vi.mock('./storage', async () => {
  const actual = await vi.importActual('./storage');
  return {
    ...actual,
    loadState: () => ({ signals: [SIG_A] }),
    saveState: vi.fn(),
  };
});

// Backend shape of SIG_A for the persistence API mock.
const SIG_A_BACKEND = {
  ...SIG_A,
  category: 'RESEARCH',
  description: SIG_A.doc || '',
};
const mockListSignals = vi.fn(() => Promise.resolve([SIG_A_BACKEND]));
vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listSignals: (...args) => mockListSignals(...args),
  createSignal: vi.fn(() => Promise.resolve({})),
  updateSignal: vi.fn(() => Promise.resolve({})),
  archiveSignal: vi.fn(() => Promise.resolve(null)),
  describePersistenceError: (err) => (err && err.message) || String(err),
}));

import SignalsPage from './SignalsPage';

// buildSignalStatsInputs requires:
//   - timestamps: unix-ms array, length ≥ 2
//   - realized_pnl: array-of-series (non-empty), each series same length as timestamps
//   - all equity values positive after: equity[i] = capital + pnl[i] * capital
const FAKE_RESULT = {
  timestamps: [1609459200000, 1609545600000, 1609632000000],
  positions: [0, 0, 0],
  events: [],
  trades: [],
  realized_pnl: [[0.001, 0.002, 0.003]],
};

beforeEach(() => {
  capturedDefaultRfr = undefined;
  capturedOnRun = null;
  capturedOnSelect = null;
  localStorage.clear();
  mockComputeSignal.mockReset();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe('<SignalsPage> — TC4.10: defaultRiskFreeRate from localStorage', () => {
  it('passes 0.05 to <Statistics> when localStorage has "5"', async () => {
    localStorage.setItem('tcg-risk-free-rate', '5');
    mockComputeSignal.mockResolvedValue(FAKE_RESULT);

    await act(async () => {
      render(<SignalsPage />);
    });

    // Statistics is not mounted until after a successful run (statsInputs non-null).
    expect(screen.queryByTestId('statistics-stub')).toBeNull();

    // Trigger a run (SIG_A is auto-selected).
    expect(capturedOnRun).not.toBeNull();
    await act(async () => {
      await capturedOnRun();
    });

    expect(screen.getByTestId('statistics-stub')).toBeTruthy();
    expect(capturedDefaultRfr).toBeCloseTo(0.05, 10);
  });

  it('passes 0.04 to <Statistics> when localStorage is empty (default)', async () => {
    localStorage.removeItem('tcg-risk-free-rate');
    mockComputeSignal.mockResolvedValue(FAKE_RESULT);

    await act(async () => {
      render(<SignalsPage />);
    });

    expect(capturedOnRun).not.toBeNull();
    await act(async () => {
      await capturedOnRun();
    });

    expect(screen.getByTestId('statistics-stub')).toBeTruthy();
    expect(capturedDefaultRfr).toBeCloseTo(0.04, 10);
  });
});
