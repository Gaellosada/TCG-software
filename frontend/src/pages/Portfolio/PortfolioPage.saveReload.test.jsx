// @vitest-environment jsdom
//
// Regression for the reported bug: "Modifying a portfolio (that has a v2 leg),
// clicking Save, then navigating elsewhere and returning shows the OLD version
// — the change (and its v2-ness) does not persist."
//
// ROOT CAUSE under test: on a successful UPDATE-save (manual Save button OR
// debounced autosave), ``handleCloudSave`` updated ``lastSeenPayloadRef`` and
// cleared dirty but NEVER refreshed the portfolios LIST cache / working copy
// (it invalidates only the per-doc DETAIL query). So the page's ``portfolios``
// working list — the source ``handleSelectPersisted`` → ``loadFromPersisted``
// reads from — retained the PRE-EDIT doc. Re-opening the same portfolio (the
// user's "navigate away and back") reloads that stale doc, silently discarding
// the saved edit. The backend HAS the new data (verified separately); the UI
// simply reads a stale cached list.
//
// This test uses a MUTABLE backend store so a proper cache refresh would
// surface the saved doc. It FAILS pre-fix (re-select shows the old weight) and
// PASSES once a successful save refreshes the working list.

import React from 'react';
import {
  describe, it, expect, vi, afterEach, beforeEach,
} from 'vitest';
import {
  render, screen, fireEvent, cleanup, act, waitFor,
} from '@testing-library/react';

let capturedUpdateLeg = null;

// HoldingsList mock that surfaces the FIRST leg's weight + dataSource so the
// test can assert exactly what the editor holds after a re-select.
vi.mock('./HoldingsList', () => ({
  default: ({ legs, onUpdateLeg }) => {
    capturedUpdateLeg = onUpdateLeg;
    return (
      <div data-testid="holdings-list">
        <span data-testid="leg-count">{legs.length}</span>
        <span data-testid="leg0-weight">{legs[0] ? String(legs[0].weight) : ''}</span>
        <span data-testid="leg0-source">{legs[0] ? String(legs[0].dataSource || 'v1') : ''}</span>
      </div>
    );
  },
}));

vi.mock('./PortfolioEquityChart', () => ({ default: () => <div /> }));
vi.mock('./ReturnsGrid', () => ({ default: () => <div /> }));
vi.mock('./AddHoldingModal', () => ({ default: () => null }));
vi.mock('./SignalPickerModal', () => ({ default: () => null }));
vi.mock('../../components/TimeRangeSlider', () => ({ default: () => <div /> }));
vi.mock('../../components/ConfirmDialog', () => ({ default: () => null }));
vi.mock('../../components/Statistics', () => ({ default: () => <div /> }));
vi.mock('../../components/TradeLog', () => ({ default: () => <div /> }));
vi.mock('../../api/data', () => ({
  getInstrumentPrices: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
  getContinuousSeries: vi.fn(() => Promise.resolve({ dates: [20200101, 20201231] })),
}));
vi.mock('../../api/statistics', () => ({
  fetchStatistics: vi.fn(() => new Promise(() => {})),
}));
vi.mock('./signalLegRange', () => ({
  fetchSignalLegRange: vi.fn(() => Promise.resolve({ id: null, start: null, end: null })),
}));

// A CONTINUOUS v2 leg in the exact shape ``legsToWire`` emits (so a freshly
// loaded doc diffs byte-identically → clean button on load).
const V2_WIRE_LEG = {
  label: 'ES', type: 'continuous', collection: 'ES', symbol: null,
  strategy: 'front_month', adjustment: 'none', cycle: 'HMUZ', rollOffset: 0,
  weight: 60, signalId: null, signalName: null, signalSpec: null,
  option_type: null, maturity: null, selection: null, stream: null,
  roll_offset: null, hold_between_rolls: false, nav_times: 1.0,
  dataSource: 'v2',
};

function freshDoc() {
  return {
    id: 'ptf-1',
    type: 'portfolio',
    name: 'My v2 Portfolio',
    category: 'RESEARCH',
    legs: [{ ...V2_WIRE_LEG }],
    rebalance: 'monthly',
    locked: false,
    kind: 'pure',
  };
}

// MUTABLE backend store — mirrors a real backend that persists PUTs and returns
// the current state on subsequent list reads.
let store;

const mockListPortfolios = vi.fn((cat) => Promise.resolve(store.filter((p) => p.category === cat)));
const mockUpdatePortfolio = vi.fn((id, body) => {
  store = store.map((p) => (p.id === id ? { ...p, ...body, id } : p));
  return Promise.resolve(store.find((p) => p.id === id));
});
const mockCreatePortfolio = vi.fn((doc) => {
  store = [...store, doc];
  return Promise.resolve(doc);
});
const mockArchivePortfolio = vi.fn(() => Promise.resolve(null));

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listPortfolios: (...a) => mockListPortfolios(...a),
  createPortfolio: (...a) => mockCreatePortfolio(...a),
  updatePortfolio: (...a) => mockUpdatePortfolio(...a),
  archivePortfolio: (...a) => mockArchivePortfolio(...a),
  getPortfolio: (id) => Promise.resolve(store.find((p) => p.id === id) || null),
  setPortfolioLocked: vi.fn(() => Promise.resolve({ ...freshDoc(), locked: true })),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: (err) => !!err && err.status === 423,
}));

import PortfolioPage from './PortfolioPage';

beforeEach(() => {
  capturedUpdateLeg = null;
  store = [freshDoc()];
  mockListPortfolios.mockClear();
  mockUpdatePortfolio.mockClear();
  mockCreatePortfolio.mockClear();
});

afterEach(() => {
  cleanup();
});

async function selectPortfolio() {
  await waitFor(() => {
    expect(screen.queryByTestId('load-portfolio-ptf-1')).not.toBeNull();
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId('load-portfolio-ptf-1'));
  });
  await waitFor(() => {
    expect(screen.getByTestId('leg-count').textContent).toBe('1');
  });
}

describe('<PortfolioPage> — a saved edit survives re-opening the portfolio', () => {
  it('edit weight → Save → re-open shows the NEW weight (not the stale pre-edit doc), v2 preserved', async () => {
    await act(async () => { render(<PortfolioPage />); });
    await selectPortfolio();

    // Loaded state: weight 60, v2 source.
    expect(screen.getByTestId('leg0-weight').textContent).toBe('60');
    expect(screen.getByTestId('leg0-source').textContent).toBe('v2');

    // Turn autosave OFF so ONLY the manual Save path is under test.
    const autosaveCb = screen.getByRole('checkbox', { name: 'Auto save' });
    await act(async () => { fireEvent.click(autosaveCb); });

    // Modify the leg weight → dirty.
    await act(async () => { capturedUpdateLeg(0, { weight: 75 }); });
    await waitFor(() => {
      expect(screen.getByTestId('leg0-weight').textContent).toBe('75');
    });

    // Click Save → the PUT persists weight 75 (with v2) to the store.
    const saveBtn = screen.getByRole('button', { name: 'Save' });
    await act(async () => { fireEvent.click(saveBtn); });
    await waitFor(() => { expect(mockUpdatePortfolio).toHaveBeenCalled(); });
    const [, body] = mockUpdatePortfolio.mock.calls[0];
    expect(body.legs[0].weight).toBe(75);
    expect(body.legs[0].dataSource).toBe('v2'); // v2 IS in the wire payload
    // Backend store now holds weight 75.
    await waitFor(() => {
      expect(store.find((p) => p.id === 'ptf-1').legs[0].weight).toBe(75);
    });

    // The user "navigates away and returns" → re-opens the SAME portfolio.
    // Re-selection reads the page's working list. If the save refreshed it,
    // the editor shows the SAVED weight 75; if it kept the stale pre-edit doc,
    // it wrongly reverts to 60 (the reported bug).
    await act(async () => {
      fireEvent.click(screen.getByTestId('load-portfolio-ptf-1'));
    });
    await waitFor(() => {
      expect(screen.getByTestId('leg0-weight').textContent).toBe('75');
    });
    expect(screen.getByTestId('leg0-source').textContent).toBe('v2');
  });
});
