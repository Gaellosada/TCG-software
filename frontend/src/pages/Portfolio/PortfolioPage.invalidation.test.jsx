// @vitest-environment jsdom
//
// Proof that PortfolioPage's invalidation is WIRED: after an archive, the
// portfolios list query refetches — and no unrelated persistence list
// (signals/indicators) refetches.

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act, waitFor } from '@testing-library/react';

vi.mock('./HoldingsList', () => ({ default: () => <div data-testid="leg-count">0</div> }));
vi.mock('./PortfolioEquityChart', () => ({ default: () => <div /> }));
vi.mock('./ReturnsGrid', () => ({ default: () => <div /> }));
vi.mock('./AddHoldingModal', () => ({ default: () => null }));
vi.mock('./SignalPickerModal', () => ({ default: () => null }));
vi.mock('../../components/SaveControls', () => ({ default: () => <div /> }));
vi.mock('../../components/TimeRangeSlider', () => ({ default: () => <div /> }));
vi.mock('../../components/ConfirmDialog', () => ({ default: () => null }));
vi.mock('../../components/Statistics', () => ({ default: () => <div /> }));
vi.mock('../../components/TradeLog', () => ({ default: () => <div /> }));

// Capture the persisted-panel callbacks to drive archive/category-change.
let capturedPanel = {};
vi.mock('./PersistedPortfolioPanel', () => ({
  default: (props) => {
    capturedPanel = props;
    return <div data-testid="persisted-panel" data-count={(props.portfolios || []).length} />;
  },
}));

vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(() => Promise.resolve({ dates: [] })),
  getContinuousSeries: vi.fn(() => Promise.resolve({ dates: [] })),
}));
vi.mock('../../api/statistics', () => ({ fetchStatistics: vi.fn(() => new Promise(() => {})) }));
vi.mock('./signalLegRange', () => ({
  fetchSignalLegRange: vi.fn(() => Promise.resolve({ id: null, start: null, end: null })),
}));

const PERSISTED_DOC = {
  id: 'ptf-1', type: 'portfolio', name: 'My Saved Portfolio', category: 'RESEARCH',
  legs: [], rebalance: 'none',
};

const mockListPortfolios = vi.fn(() => Promise.resolve([PERSISTED_DOC]));
const mockListSignals = vi.fn(() => Promise.resolve([]));
const mockListIndicators = vi.fn(() => Promise.resolve([]));
const mockArchivePortfolio = vi.fn(() => Promise.resolve(null));

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listPortfolios: (...a) => mockListPortfolios(...a),
  listSignals: (...a) => mockListSignals(...a),
  listIndicators: (...a) => mockListIndicators(...a),
  createPortfolio: vi.fn(() => Promise.resolve({ ...PERSISTED_DOC })),
  updatePortfolio: vi.fn(() => Promise.resolve({ ...PERSISTED_DOC })),
  archivePortfolio: (...a) => mockArchivePortfolio(...a),
  setPortfolioLocked: vi.fn(() => Promise.resolve({})),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: (err) => !!err && err.status === 423,
}));

import PortfolioPage from './PortfolioPage';

beforeEach(() => {
  capturedPanel = {};
  mockListPortfolios.mockReset().mockResolvedValue([PERSISTED_DOC]);
  mockListSignals.mockReset().mockResolvedValue([]);
  mockListIndicators.mockReset().mockResolvedValue([]);
  mockArchivePortfolio.mockReset().mockResolvedValue(null);
});
afterEach(cleanup);

describe('PortfolioPage — invalidation is wired (C3)', () => {
  it('refetches the portfolios list after an archive, touching no other resource', async () => {
    await act(async () => { render(<PortfolioPage />); });

    await waitFor(() => expect(mockListPortfolios).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(capturedPanel.onArchive).toBeTypeOf('function'));
    const callsBefore = mockListPortfolios.mock.calls.length;

    // Trigger archive via the captured panel callback.
    await act(async () => {
      await capturedPanel.onArchive('ptf-1');
    });

    expect(mockArchivePortfolio).toHaveBeenCalledWith('ptf-1');
    await waitFor(() => expect(mockListPortfolios.mock.calls.length).toBe(callsBefore + 1));
    // Isolation: the portfolio edit must not refetch signals or indicators.
    expect(mockListSignals).not.toHaveBeenCalled();
    expect(mockListIndicators).not.toHaveBeenCalled();
  });
});
