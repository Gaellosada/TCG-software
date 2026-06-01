// @vitest-environment jsdom
//
// B2 regression: re-clicking the same already-selected persisted signal
// must NOT overwrite in-progress edits with the stale backend snapshot.
// (Before the fix, ``handleSelectPersisted`` unconditionally re-hydrated
// from the backend doc — which silently discarded edits typed since the
// last debounced save.)

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import {
  render, screen, fireEvent, cleanup, act, waitFor,
} from '@testing-library/react';

// --- Mocks --------------------------------------------------------------

let capturedOnRulesChange = null;
let lastRulesProp = null;

vi.mock('./SignalsList', () => ({
  default: ({ signals, onSelect }) => (
    <div data-testid="signals-list">
      {signals.map((s) => (
        <button
          key={s.id}
          data-testid={`select-${s.id}`}
          type="button"
          onClick={() => onSelect(s.id)}
        >
          {s.name}
        </button>
      ))}
    </div>
  ),
}));

vi.mock('./BlockEditor', () => ({
  default: ({ rules, onRulesChange }) => {
    capturedOnRulesChange = onRulesChange;
    lastRulesProp = rules;
    return (
      <div data-testid="block-editor-stub">
        <span data-testid="rules-entry-name">
          {(rules?.entries || [])[0]?.name || ''}
        </span>
      </div>
    );
  },
}));

vi.mock('./ParamsPanel', () => ({
  default: () => <div data-testid="params-panel-stub" />,
}));
vi.mock('./InputsPanel', () => ({
  default: () => <div data-testid="inputs-panel-stub" />,
}));
vi.mock('./ResultsView', () => ({
  default: () => <div data-testid="results-view-stub" />,
}));
vi.mock('../../components/Statistics', () => ({
  default: () => <div data-testid="statistics-stub" />,
}));
vi.mock('../../components/TradeLog', () => ({
  default: () => <div data-testid="trade-log-stub" />,
}));
vi.mock('./hydrateIndicators', () => ({
  hydrateAvailableIndicators: () => Promise.resolve([]),
}));
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

const PERSISTED_DOC = {
  id: 'sig-reselect-1',
  type: 'signal',
  name: 'Re-select Signal',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  inputs: [],
  rules: {
    entries: [{ id: 'b1', name: 'Original Block Name', input_id: 'X', weight: 50, conditions: [] }],
    exits: [],
    resets: [],
  },
  settings: { dont_repeat: true },
  description: '',
};

const mockUpdateSignal = vi.fn(() => Promise.resolve({ ...PERSISTED_DOC }));
const mockListSignals = vi.fn(() => Promise.resolve([PERSISTED_DOC]));
const mockCreateSignal = vi.fn(() => Promise.resolve({ ...PERSISTED_DOC }));

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  createSignal: (...args) => mockCreateSignal(...args),
  listSignals: (...args) => mockListSignals(...args),
  updateSignal: (...args) => mockUpdateSignal(...args),
  archiveSignal: vi.fn(() => Promise.resolve(null)),
  describePersistenceError: (err) => (err && err.message) || String(err),
}));

import SignalsPage from './SignalsPage';

beforeEach(() => {
  capturedOnRulesChange = null;
  lastRulesProp = null;
  mockUpdateSignal.mockClear();
  mockListSignals.mockClear();
  mockCreateSignal.mockClear();
});

afterEach(() => {
  cleanup();
});

describe('<SignalsPage> — re-select guard (B2)', () => {
  it('re-clicking the same already-selected row preserves in-progress edits before the debounced save fires', async () => {
    vi.useFakeTimers();
    try {
      await act(async () => {
        render(<SignalsPage />);
      });
      // Let list fetch settle.
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Select the persisted signal.
      await act(async () => {
        fireEvent.click(screen.getByTestId('select-sig-reselect-1'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // Verify hydrate happened.
      expect(screen.getByTestId('rules-entry-name').textContent).toBe('Original Block Name');
      expect(capturedOnRulesChange).not.toBeNull();

      // User types — edits the rules locally. The autosave debounce
      // (3s) has NOT fired yet.
      const editedRules = {
        ...lastRulesProp,
        entries: [
          { ...lastRulesProp.entries[0], name: 'IN-PROGRESS EDIT' },
        ],
      };
      await act(async () => {
        capturedOnRulesChange(editedRules);
      });

      // Confirm the edit is reflected in the editor stub.
      expect(screen.getByTestId('rules-entry-name').textContent).toBe('IN-PROGRESS EDIT');

      // User clicks the SAME row again BEFORE the debounce has elapsed.
      // Without the guard, the old behaviour would overwrite the local
      // edit with the stale backend snapshot.
      await act(async () => {
        fireEvent.click(screen.getByTestId('select-sig-reselect-1'));
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      // The edit must still be present.
      expect(screen.getByTestId('rules-entry-name').textContent).toBe('IN-PROGRESS EDIT');

      // updateSignal must NOT have been called yet (debounce hasn't fired).
      expect(mockUpdateSignal).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
