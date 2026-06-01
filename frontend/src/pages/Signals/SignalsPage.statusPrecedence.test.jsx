// @vitest-environment jsdom
//
// M7 regression: ``oneshotStatus`` must NOT permanently mask
// ``cloudStatus``. When the debounced cloud autosave transitions to
// ``'saving'``, it must take precedence over any stale ``'saved'`` from
// a recent one-shot operation. The user must always see honest state.
//
// M8 regression: error messages from the persistence layer must reach
// the SaveStatus component (as ``errorMessage`` prop / tooltip) — not
// be discarded by bare ``catch {}`` blocks.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import {
  render, screen, fireEvent, cleanup, act, waitFor,
} from '@testing-library/react';

let capturedOnRulesChange = null;
let lastRulesProp = null;

vi.mock('./SignalsList', () => ({
  default: ({ signals, onSelect, onAdd, onChangeItemCat }) => (
    <div data-testid="signals-list">
      <button data-testid="add-signal-btn" type="button" onClick={onAdd}>+ New</button>
      {signals.map((s) => (
        <div key={s.id}>
          <button
            data-testid={`select-${s.id}`}
            type="button"
            onClick={() => onSelect(s.id)}
          >
            {s.name}
          </button>
          <button
            data-testid={`cat-${s.id}`}
            type="button"
            onClick={() => onChangeItemCat(s.id, 'DEV')}
          >
            move
          </button>
        </div>
      ))}
    </div>
  ),
}));

vi.mock('./BlockEditor', () => ({
  default: ({ rules, onRulesChange }) => {
    capturedOnRulesChange = onRulesChange;
    lastRulesProp = rules;
    return <div data-testid="block-editor-stub" />;
  },
}));
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
vi.mock('./runGate', () => ({
  computeRunGate: () => ({ runDisabledReason: 'no signal', missingIds: [] }),
}));
vi.mock('./requestBuilder', () => ({
  buildComputeRequestBody: () => ({ body: { spec: {}, indicators: [] }, missing: [] }),
}));
vi.mock('./storage', () => ({
  loadState: () => ({ signals: [] }),
  saveState: vi.fn(),
  emptyRules: () => ({ entries: [], exits: [], resets: [] }),
  defaultSettings: () => ({ dont_repeat: true }),
}));

// ConfirmDialog mock — auto-confirms.
vi.mock('../../components/ConfirmDialog', () => ({
  default: ({ open, onConfirm }) => (open ? (
    <div data-testid="confirm-dialog">
      <button data-testid="confirm-btn" type="button" onClick={onConfirm}>OK</button>
    </div>
  ) : null),
}));

const PERSISTED_DOC = {
  id: 'sig-prec-1',
  type: 'signal',
  name: 'Prec Signal',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  inputs: [],
  rules: { entries: [{ id: 'b1', name: 'B1', input_id: 'X', weight: 50, conditions: [] }], exits: [], resets: [] },
  settings: { dont_repeat: true },
  description: '',
};

const mockCreateSignal = vi.fn();
const mockListSignals = vi.fn();
const mockUpdateSignal = vi.fn();
const mockArchiveSignal = vi.fn();

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  createSignal: (...args) => mockCreateSignal(...args),
  listSignals: (...args) => mockListSignals(...args),
  updateSignal: (...args) => mockUpdateSignal(...args),
  archiveSignal: (...args) => mockArchiveSignal(...args),
  describePersistenceError: (err) => {
    if (!err) return 'Unknown error';
    const status = err.status;
    const msg = err.message || String(err);
    if (typeof status !== 'number') return msg;
    if (status === 409) return `Conflict (409): ${msg}`;
    if (status === 413) return `Payload too large (413): ${msg}`;
    if (status === 422) return `Validation error (422): ${msg}`;
    if (status >= 400 && status < 500) return `Client error (${status}): ${msg}`;
    if (status >= 500) return `Server error (${status}): ${msg}`;
    return msg;
  },
}));

import SignalsPage from './SignalsPage';

beforeEach(() => {
  capturedOnRulesChange = null;
  lastRulesProp = null;
  mockCreateSignal.mockReset();
  mockListSignals.mockReset();
  mockUpdateSignal.mockReset();
  mockArchiveSignal.mockReset();
  mockListSignals.mockResolvedValue([PERSISTED_DOC]);
  mockUpdateSignal.mockResolvedValue({ ...PERSISTED_DOC });
  mockArchiveSignal.mockResolvedValue(null);
  mockCreateSignal.mockResolvedValue({ ...PERSISTED_DOC, id: 'sig-new' });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// M7 — precedence
// ---------------------------------------------------------------------------
describe('<SignalsPage> M7 — cloudStatus saving wins over stale oneshot saved', () => {
  it('after a successful one-shot category change, debounced autosave saving must surface', async () => {
    vi.useFakeTimers();
    // updateSignal: first call (category change) resolves immediately;
    // second call (autosave PUT) hangs so 'saving' stays visible.
    let callCount = 0;
    mockUpdateSignal.mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) return Promise.resolve({ ...PERSISTED_DOC, category: 'DEV' });
      return new Promise(() => {}); // never resolves
    });
    try {
      await act(async () => { render(<SignalsPage />); });
      // Let the list-fetch microtask settle.
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Select the signal so SaveStatus is rendered.
      expect(screen.queryByTestId('select-sig-prec-1')).not.toBeNull();
      await act(async () => {
        fireEvent.click(screen.getByTestId('select-sig-prec-1'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Trigger a category change — resolves immediately to 'saved'.
      await act(async () => {
        fireEvent.click(screen.getByTestId('cat-sig-prec-1'));
        await vi.advanceTimersByTimeAsync(0);
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Now oneshotStatus should be 'saved'.
      let el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('saved');

      // Type into rules — kicks off the debounced backend autosave.
      const editedRules = {
        ...lastRulesProp,
        entries: [{ ...lastRulesProp.entries[0], name: 'EDITED' }],
      };
      await act(async () => { capturedOnRulesChange(editedRules); });

      // Advance past the 3s debounce — autosave fires (second updateSignal call: hangs).
      await act(async () => { await vi.advanceTimersByTimeAsync(3100); });

      // SaveStatus must now show 'saving' (cloud autosave in flight),
      // NOT the stale 'saved' from the one-shot category change.
      el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('saving');
    } finally {
      vi.useRealTimers();
    }
  });
});

// ---------------------------------------------------------------------------
// M8 — error messages reach SaveStatus
// ---------------------------------------------------------------------------
describe('<SignalsPage> M8 — error message surfacing on SaveStatus', () => {
  const errorCases = [
    { status: 409, message: 'duplicate id', expected: 'Conflict (409): duplicate id' },
    { status: 422, message: 'invalid payload', expected: 'Validation error (422): invalid payload' },
    { status: 413, message: 'too big', expected: 'Payload too large (413): too big' },
    { status: 500, message: 'kaboom', expected: 'Server error (500): kaboom' },
  ];

  it.each(errorCases)('surfaces $expected to SaveStatus on createSignal $status failure', async ({ status, message, expected }) => {
    const origConsoleError = console.error;
    console.error = () => {}; // suppress expected error log
    try {
      const err = new Error(message);
      err.status = status;
      mockCreateSignal.mockRejectedValue(err);

      await act(async () => { render(<SignalsPage />); });
      await act(async () => {});

      // Click "+ New"
      await act(async () => {
        fireEvent.click(screen.getByTestId('add-signal-btn'));
      });

      await waitFor(() => {
        const el = screen.queryByTestId('save-status');
        expect(el).not.toBeNull();
        expect(el.dataset.status).toBe('error');
      });
      const el = screen.getByTestId('save-status');
      // The error detail must be exposed as data-error-message AND as
      // the title attribute (tooltip).
      expect(el.dataset.errorMessage).toBe(expected);
      expect(el.getAttribute('title')).toBe(expected);
      // Visible inline subtext also exposes the detail.
      const detail = screen.queryByTestId('save-status-error-detail');
      expect(detail).not.toBeNull();
      expect(detail.textContent).toContain(expected);
    } finally {
      console.error = origConsoleError;
    }
  });
});
