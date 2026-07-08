// @vitest-environment jsdom
//
// Regression for the reported "Save does nothing" bug on the Portfolio page:
//
//   With autosave OFF, after editing a leg the SaveControls button shows
//   "Unsaved changes" and reads SOLID. Clicking Save fires the PUT (data IS
//   persisted) but the UI never reflected it — the button stayed solid and
//   "Unsaved changes" never cleared, because usePortfolio's ``dirty`` flag
//   was set true on every edit and reset only on load/clear, NEVER on save.
//
// This test uses the REAL SaveControls (not the no-op mock other suites use)
// so it asserts the actual button + label state the user sees. It FAILS
// against the pre-fix code and PASSES once a successful save clears dirty.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react';

let capturedUpdateLeg = null;

vi.mock('./HoldingsList', () => ({
  default: ({ legs, onUpdateLeg }) => {
    capturedUpdateLeg = onUpdateLeg;
    return (
      <div data-testid="holdings-list">
        <span data-testid="leg-count">{legs.length}</span>
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

// Wire-shaped legs (all fields present) so the loaded snapshot matches the
// serialized payload — mirrors a real backend doc round-tripped through
// legsToWire (which is how persisted portfolios are actually stored).
const WIRE_LEG = {
  label: 'SPY', type: 'instrument', collection: 'spot_daily', symbol: 'SPY',
  strategy: null, adjustment: null, cycle: null, rollOffset: 0, weight: 60,
  signalId: null, signalName: null, signalSpec: null, option_type: null,
  maturity: null, selection: null, stream: null, roll_offset: null,
  hold_between_rolls: false, nav_times: 1.0,
};

const PERSISTED_DOC = {
  id: 'ptf-1',
  type: 'portfolio',
  name: 'My Saved Portfolio',
  category: 'RESEARCH',
  legs: [WIRE_LEG],
  rebalance: 'monthly',
  locked: false,
};

const mockListPortfolios = vi.fn(() => Promise.resolve([PERSISTED_DOC]));
const mockUpdatePortfolio = vi.fn(() => Promise.resolve({ ...PERSISTED_DOC }));
const mockCreatePortfolio = vi.fn(() => Promise.resolve({ ...PERSISTED_DOC }));
const mockArchivePortfolio = vi.fn(() => Promise.resolve(null));

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listPortfolios: (...a) => mockListPortfolios(...a),
  createPortfolio: (...a) => mockCreatePortfolio(...a),
  updatePortfolio: (...a) => mockUpdatePortfolio(...a),
  archivePortfolio: (...a) => mockArchivePortfolio(...a),
  setPortfolioLocked: vi.fn(() => Promise.resolve({ ...PERSISTED_DOC, locked: true })),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: (err) => !!err && err.status === 423,
}));

import PortfolioPage from './PortfolioPage';

beforeEach(() => {
  capturedUpdateLeg = null;
  mockListPortfolios.mockClear();
  mockUpdatePortfolio.mockClear();
  mockCreatePortfolio.mockClear();
  mockListPortfolios.mockResolvedValue([PERSISTED_DOC]);
});

afterEach(() => {
  cleanup();
});

async function loadPortfolio() {
  await act(async () => { render(<PortfolioPage />); });
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

describe('<PortfolioPage> — manual Save clears dirty + reflects in the UI', () => {
  it('with autosave OFF: editing a leg then clicking Save fires the PUT AND clears the dirty UI', async () => {
    await loadPortfolio();

    const saveBtn = screen.getByRole('button', { name: 'Save' });
    // Right after load nothing is dirty — button is clean.
    await waitFor(() => {
      expect(saveBtn.getAttribute('data-clean')).toBe('true');
    });

    // Turn autosave OFF (the reported scenario). It starts checked.
    const autosaveCb = screen.getByRole('checkbox', { name: 'Auto save' });
    await act(async () => { fireEvent.click(autosaveCb); });
    expect(autosaveCb.checked).toBe(false);

    // Edit a leg → dirty. The button goes solid and "Unsaved changes" shows.
    await act(async () => { capturedUpdateLeg(0, { weight: 75 }); });
    await waitFor(() => {
      expect(saveBtn.getAttribute('data-clean')).toBe('false');
    });
    expect(screen.queryByText('Unsaved changes')).not.toBeNull();

    mockUpdatePortfolio.mockClear();

    // Click Save.
    await act(async () => { fireEvent.click(saveBtn); });

    // (a) The PUT fires with the edited leg — data IS persisted.
    await waitFor(() => {
      expect(mockUpdatePortfolio).toHaveBeenCalled();
    });
    const [calledId, body] = mockUpdatePortfolio.mock.calls[0];
    expect(calledId).toBe('ptf-1');
    expect(body.legs[0].weight).toBe(75);

    // (b) After the save resolves the UI reflects it: the button goes clean
    //     (transparent) and "Unsaved changes" clears. THIS is what regressed.
    await waitFor(() => {
      expect(saveBtn.getAttribute('data-clean')).toBe('true');
    });
    expect(screen.queryByText('Unsaved changes')).toBeNull();
  });

  it('editing a leg then reverting it to the saved value clears the dirty UI (no ghost "Unsaved changes")', async () => {
    // FE-SAVE-1: the button's ``dirty`` prop was driven by usePortfolio's
    // MONOTONIC ``dirty`` flag, but the autosave enable-gate + markSaved fire
    // off the true content-diff (cloudDirty). Edit a leg then revert it to the
    // saved value BEFORE the debounce fires: cloudDirty recomputes false, so
    // autosave never fires and markSaved is never called — the monotonic flag
    // stays true forever and the button falsely shows "Unsaved changes" on
    // byte-identical content. Drive the button off the same content-diff.
    // Autosave stays ON — the revert happens BEFORE the 3s debounce fires, so
    // no save ever runs (real timers; the test completes in ms).
    await loadPortfolio();

    const saveBtn = screen.getByRole('button', { name: 'Save' });
    await waitFor(() => {
      expect(saveBtn.getAttribute('data-clean')).toBe('true');
    });

    // Edit → dirty (button solid).
    await act(async () => { capturedUpdateLeg(0, { weight: 75 }); });
    await waitFor(() => {
      expect(saveBtn.getAttribute('data-clean')).toBe('false');
    });

    // Revert to the saved value (60) — content is now byte-identical to the
    // persisted snapshot. No save has fired. The button MUST go clean.
    await act(async () => { capturedUpdateLeg(0, { weight: 60 }); });
    await waitFor(() => {
      expect(saveBtn.getAttribute('data-clean')).toBe('true');
    });
    // No redundant PUT was needed to reach the clean state.
    expect(mockUpdatePortfolio).not.toHaveBeenCalled();
  });

  it('re-editing after a save marks dirty again', async () => {
    await loadPortfolio();
    const saveBtn = screen.getByRole('button', { name: 'Save' });
    const autosaveCb = screen.getByRole('checkbox', { name: 'Auto save' });
    await act(async () => { fireEvent.click(autosaveCb); });

    await act(async () => { capturedUpdateLeg(0, { weight: 75 }); });
    await act(async () => { fireEvent.click(saveBtn); });
    await waitFor(() => {
      expect(saveBtn.getAttribute('data-clean')).toBe('true');
    });

    // A fresh edit must re-dirty the button.
    await act(async () => { capturedUpdateLeg(0, { weight: 80 }); });
    await waitFor(() => {
      expect(saveBtn.getAttribute('data-clean')).toBe('false');
    });
  });
});
