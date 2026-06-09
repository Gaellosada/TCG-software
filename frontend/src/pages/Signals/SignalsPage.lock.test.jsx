// @vitest-environment jsdom
//
// Feature 2 (lock) wiring for SignalsPage:
//   1. The lock handler passed to SignalsList calls setSignalLocked(id,
//      next) and patches the row's `locked` flag from the returned doc.
//   2. When the CURRENTLY-LOADED signal is locked, the editor is
//      read-only: a lock banner is shown and the Save button is disabled.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, act, waitFor, fireEvent } from '@testing-library/react';

// SignalsList stub exposes the lock handler + current locked flag per row
// so the test can drive handleSetSignalLocked imperatively and observe the
// resulting state patch.
let capturedOnSetSignalLocked = null;
vi.mock('./SignalsList', () => ({
  default: ({ signals, onSelect, onSetSignalLocked }) => {
    capturedOnSetSignalLocked = onSetSignalLocked;
    return (
      <div data-testid="signals-list">
        {signals.map((s) => (
          <div key={s.id} data-testid={`row-${s.id}`} data-locked={s.locked ? 'true' : 'false'}>
            <button type="button" data-testid={`select-${s.id}`} onClick={() => onSelect(s.id)}>
              {s.name}
            </button>
          </div>
        ))}
      </div>
    );
  },
}));

vi.mock('./BlockEditor', () => ({ default: () => <div data-testid="block-editor-stub" /> }));
vi.mock('./ParamsPanel', () => ({ default: () => <div data-testid="params-panel-stub" /> }));
vi.mock('./InputsPanel', () => ({ default: () => <div data-testid="inputs-panel-stub" /> }));
vi.mock('./ResultsView', () => ({ default: () => <div data-testid="results-view-stub" /> }));
vi.mock('../../components/Statistics', () => ({ default: () => <div data-testid="statistics-stub" /> }));
vi.mock('../../components/TradeLog', () => ({ default: () => <div data-testid="trade-log-stub" /> }));
vi.mock('./hydrateIndicators', () => ({ hydrateAvailableIndicators: () => Promise.resolve([]) }));
vi.mock('../../api/signals', () => ({
  computeSignal: vi.fn(),
  collectIndicatorIds: () => new Set(),
}));

const mockSetSignalLocked = vi.fn();
const mockListSignals = vi.fn();
const mockUpdateSignal = vi.fn();
vi.mock('../../api/persistence', () => ({
  listSignals: (...a) => mockListSignals(...a),
  createSignal: vi.fn(() => Promise.resolve({})),
  updateSignal: (...a) => mockUpdateSignal(...a),
  archiveSignal: vi.fn(() => Promise.resolve(null)),
  setSignalLocked: (...a) => mockSetSignalLocked(...a),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: (err) => !!err && err.status === 423,
}));

import SignalsPage from './SignalsPage';

function persisted(over = {}) {
  return {
    id: 's1', name: 'Sig 1', inputs: [], rules: { entries: [], exits: [] },
    settings: { dont_repeat: true }, description: '', locked: false, ...over,
  };
}

afterEach(() => { cleanup(); capturedOnSetSignalLocked = null; vi.clearAllMocks(); try { localStorage.clear(); } catch { /* ignore */ } });
beforeEach(() => {
  mockSetSignalLocked.mockReset();
  mockListSignals.mockReset();
  mockUpdateSignal.mockReset();
  mockUpdateSignal.mockResolvedValue({});
});

describe('SignalsPage — lock wiring', () => {
  it('handleSetSignalLocked calls setSignalLocked and patches the row locked flag', async () => {
    mockListSignals.mockResolvedValue([persisted({ id: 's1', locked: false })]);
    mockSetSignalLocked.mockResolvedValue(persisted({ id: 's1', locked: true }));
    render(<SignalsPage />);
    await screen.findByTestId('row-s1');
    expect(screen.getByTestId('row-s1').getAttribute('data-locked')).toBe('false');

    await act(async () => { await capturedOnSetSignalLocked('s1', true); });

    expect(mockSetSignalLocked).toHaveBeenCalledWith('s1', true);
    await waitFor(() => {
      expect(screen.getByTestId('row-s1').getAttribute('data-locked')).toBe('true');
    });
  });

  it('shows a read-only lock banner and disables Save when the loaded signal is locked', async () => {
    mockListSignals.mockResolvedValue([persisted({ id: 's1', locked: true })]);
    render(<SignalsPage />);
    // The first (and only) signal auto-selects; it is locked.
    await screen.findByTestId('signal-lock-banner');
    expect(screen.getByTestId('signal-lock-banner').textContent).toMatch(/locked/i);
    // Save button (shared SaveControls) is disabled.
    expect(screen.getByRole('button', { name: 'Save' }).disabled).toBe(true);
  });

  it('does NOT show the lock banner when the loaded signal is unlocked', async () => {
    mockListSignals.mockResolvedValue([persisted({ id: 's1', locked: false })]);
    render(<SignalsPage />);
    await screen.findByTestId('inputs-panel-stub');
    expect(screen.queryByTestId('signal-lock-banner')).toBeNull();
  });
});

describe('SignalsPage — 423 on save flips to read-only', () => {
  // Manual-save path (autosave OFF) calls handleBackendSave directly, so we
  // can drive a save synchronously without waiting on the 3s debounce.
  function lockedError() {
    const e = new Error('Document is locked');
    e.status = 423;
    return e;
  }

  it('a save rejected with 423 flips the page to read-only + shows the lock banner', async () => {
    localStorage.setItem('tcg.signals.autosave', 'false');
    mockListSignals.mockResolvedValue([persisted({ id: 's1', locked: false })]);
    mockUpdateSignal.mockRejectedValue(lockedError());
    render(<SignalsPage />);
    await screen.findByTestId('inputs-panel-stub');
    // Initially unlocked: no banner, Save enabled.
    expect(screen.queryByTestId('signal-lock-banner')).toBeNull();
    const saveBtn = screen.getByRole('button', { name: 'Save' });

    await act(async () => { fireEvent.click(saveBtn); });

    expect(mockUpdateSignal).toHaveBeenCalled();
    // The 423 flips the LOCAL locked flag → banner + read-only Save.
    await screen.findByTestId('signal-lock-banner');
    expect(screen.getByRole('button', { name: 'Save' }).disabled).toBe(true);
  });

  it('a NON-locked save error (500) does NOT flip to read-only', async () => {
    localStorage.setItem('tcg.signals.autosave', 'false');
    mockListSignals.mockResolvedValue([persisted({ id: 's1', locked: false })]);
    const e = new Error('boom');
    e.status = 500;
    mockUpdateSignal.mockRejectedValue(e);
    render(<SignalsPage />);
    await screen.findByTestId('inputs-panel-stub');
    const saveBtn = screen.getByRole('button', { name: 'Save' });

    await act(async () => { fireEvent.click(saveBtn); });

    expect(mockUpdateSignal).toHaveBeenCalled();
    // Flush any pending state from the rejection.
    await act(async () => {});
    // No lock banner — a generic (non-423) error must NOT flip to read-only.
    expect(screen.queryByTestId('signal-lock-banner')).toBeNull();
  });
});
