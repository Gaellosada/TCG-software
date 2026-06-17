// @vitest-environment jsdom
//
// Proof that IndicatorsPage's invalidation is WIRED: after a create/archive,
// the indicators list query refetches exactly once — and no unrelated
// persistence list (signals/portfolios) refetches.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, act, waitFor } from '@testing-library/react';

vi.mock('../../components/Chart', () => ({ default: () => <div data-testid="chart-stub" /> }));

// Capture IndicatorsList callbacks to drive add/delete.
let capturedProps = {};
vi.mock('./IndicatorsList', () => ({
  default: (props) => {
    capturedProps = props;
    return <div data-testid="indicators-list-stub" data-count={(props.indicators || []).length} />;
  },
}));
vi.mock('./EditorPanel', () => ({ default: () => <div /> }));
vi.mock('./ParamsPanel', () => ({ default: () => <div /> }));
vi.mock('./IndicatorChart', () => ({ default: () => <div /> }));

const computeIndicatorMock = vi.fn();
const resolveDefaultIndexInstrumentMock = vi.fn(() => Promise.resolve({ ok: true, data: null }));
vi.mock('../../api/indicators', () => ({
  computeIndicator: (...a) => computeIndicatorMock(...a),
  resolveDefaultIndexInstrument: (...a) => resolveDefaultIndexInstrumentMock(...a),
}));
vi.mock('../../api/options', () => ({
  getOptionRoots: vi.fn(() => Promise.resolve({ roots: [] })),
}));

// Single hardcoded default so the page has a stable readonly entry; custom
// (backend) indicators come from the mocked list.
vi.mock('./defaultIndicators', () => ({
  DEFAULT_INDICATORS: [{
    id: 'sma', name: 'SMA', readonly: true, category: 'trend',
    code: "def compute(series, window: int = 20):\n    s = series['close']\n    return s",
    params: { window: 20 }, seriesMap: {}, doc: '', ownPanel: false,
  }],
}));

const mockListIndicators = vi.fn(() => Promise.resolve([]));
const mockListSignals = vi.fn(() => Promise.resolve([]));
const mockListPortfolios = vi.fn(() => Promise.resolve([]));
const mockCreateIndicator = vi.fn(() => Promise.resolve({}));
const mockArchiveIndicator = vi.fn(() => Promise.resolve(null));
vi.mock('../../api/persistence', () => ({
  listIndicators: (...a) => mockListIndicators(...a),
  listSignals: (...a) => mockListSignals(...a),
  listPortfolios: (...a) => mockListPortfolios(...a),
  createIndicator: (...a) => mockCreateIndicator(...a),
  updateIndicator: vi.fn(() => Promise.resolve({})),
  archiveIndicator: (...a) => mockArchiveIndicator(...a),
  setIndicatorLocked: vi.fn(() => Promise.resolve({})),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: () => false,
}));

import IndicatorsPage from './IndicatorsPage';

beforeEach(() => {
  capturedProps = {};
  mockListIndicators.mockReset().mockResolvedValue([]);
  mockListSignals.mockReset().mockResolvedValue([]);
  mockListPortfolios.mockReset().mockResolvedValue([]);
  mockCreateIndicator.mockReset().mockResolvedValue({});
  mockArchiveIndicator.mockReset().mockResolvedValue(null);
  try { localStorage.clear(); } catch { /* ignore */ }
});
afterEach(cleanup);

describe('IndicatorsPage — invalidation is wired (C3)', () => {
  it('refetches the indicators list exactly once after a create, touching no other resource', async () => {
    render(<IndicatorsPage />);

    await waitFor(() => expect(mockListIndicators).toHaveBeenCalledTimes(1));
    expect(capturedProps.onAdd).toBeTypeOf('function');
    const callsBefore = mockListIndicators.mock.calls.length;

    await act(async () => {
      await capturedProps.onAdd();
    });

    expect(mockCreateIndicator).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(mockListIndicators.mock.calls.length).toBe(callsBefore + 1));
    // Isolation.
    expect(mockListSignals).not.toHaveBeenCalled();
    expect(mockListPortfolios).not.toHaveBeenCalled();
  });

  it('refetches the indicators list exactly once after an archive', async () => {
    // Seed one custom (deletable) backend indicator.
    mockListIndicators.mockResolvedValue([
      { id: 'cust-1', name: 'Custom 1', definition: { code: "def compute(series):\n    return series['close']", params: {}, seriesMap: {} } },
    ]);

    render(<IndicatorsPage />);
    await waitFor(() => expect(mockListIndicators).toHaveBeenCalledTimes(1));
    // Wait until the custom indicator has merged into the list prop.
    await waitFor(() => expect(capturedProps.onDelete).toBeTypeOf('function'));
    const callsBefore = mockListIndicators.mock.calls.length;

    // onDelete opens a confirm dialog; confirm it.
    await act(async () => {
      capturedProps.onDelete('cust-1');
    });
    const confirmBtn = await screen.findByRole('button', { name: /delete|confirm|archive/i });
    await act(async () => { confirmBtn.click(); });

    expect(mockArchiveIndicator).toHaveBeenCalledWith('cust-1');
    await waitFor(() => expect(mockListIndicators.mock.calls.length).toBe(callsBefore + 1));
    expect(mockListSignals).not.toHaveBeenCalled();
    expect(mockListPortfolios).not.toHaveBeenCalled();
  });
});
