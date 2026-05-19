// @vitest-environment jsdom
//
// Backend-persistence round-trip:
//
//   1. Mount SignalsPage with mock persistence API.
//   2. Backend "list" returns one signal with NON-EMPTY rules.
//   3. User selects that signal — assert the editor receives the
//      hydrated rules (the load-bearing fix for the rules-don't-persist
//      bug).
//   4. User edits the rules — within ~600ms the page calls
//      ``updateSignal`` with the new payload including the edited rules.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react';

// --- Mocks ---------------------------------------------------------------

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
        <span data-testid="rules-entry-count">
          {(rules?.entries || []).length}
        </span>
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
  default: ({ inputs }) => (
    <div data-testid="inputs-panel-stub">
      <span data-testid="inputs-count">{(inputs || []).length}</span>
    </div>
  ),
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
  hydrateAvailableIndicators: () => [],
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
  id: 'sig-persisted-1',
  type: 'signal',
  name: 'My Saved Signal',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  inputs: [{ id: 'X', instrument: { type: 'spot', collection: 'spot_daily', instrument_id: 'AAPL' } }],
  rules: {
    entries: [{ id: 'b1', name: 'My Entry Block', input_id: 'X', weight: 50, conditions: [] }],
    exits: [],
    resets: [],
  },
  settings: { dont_repeat: true },
  description: 'hello world',
};

const mockUpdateSignal = vi.fn(() => Promise.resolve({ ...PERSISTED_DOC }));
const mockListSignals = vi.fn(() => Promise.resolve([PERSISTED_DOC]));
const mockCreateSignal = vi.fn(() => Promise.resolve({ ...PERSISTED_DOC, id: 'sig-new' }));

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
  mockListSignals.mockResolvedValue([PERSISTED_DOC]);
});

afterEach(() => {
  cleanup();
});

describe('<SignalsPage> — backend hydrate + autosave', () => {
  it('hydrates the editor from the backend doc when a persisted signal is selected', async () => {
    await act(async () => {
      render(<SignalsPage />);
    });

    // Wait for the persisted list to surface in the UI.
    await waitFor(() => {
      expect(screen.queryByTestId('select-sig-persisted-1')).not.toBeNull();
    });

    // Select the persisted signal.
    await act(async () => {
      fireEvent.click(screen.getByTestId('select-sig-persisted-1'));
    });

    // The editor should now show the hydrated rules (1 entry) and inputs (1).
    await waitFor(() => {
      expect(screen.getByTestId('rules-entry-count').textContent).toBe('1');
      expect(screen.getByTestId('rules-entry-name').textContent).toBe('My Entry Block');
      expect(screen.getByTestId('inputs-count').textContent).toBe('1');
    });
  });

  it('PUTs the edited rules to the backend within ~3100ms', async () => {
    // Use fake timers for the debounce window — real timers would make
    // this test slow / flaky.
    vi.useFakeTimers();
    try {
      await act(async () => {
        render(<SignalsPage />);
      });
      // Let pending microtasks (list fetch) settle.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      // Select the persisted signal.
      await act(async () => {
        fireEvent.click(screen.getByTestId('select-sig-persisted-1'));
      });
      // Allow seeding effect to run.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      mockUpdateSignal.mockClear();

      // Edit the rules by invoking the captured onRulesChange handler.
      expect(capturedOnRulesChange).not.toBeNull();
      const editedRules = {
        ...lastRulesProp,
        entries: [
          { ...lastRulesProp.entries[0], name: 'Edited Entry Block' },
        ],
      };
      await act(async () => {
        capturedOnRulesChange(editedRules);
      });

      // Within the debounce window the PUT must have fired.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3100);
      });

      expect(mockUpdateSignal).toHaveBeenCalled();
      // The PUT payload must carry the edited rules.
      const [calledId, calledBody] = mockUpdateSignal.mock.calls[0];
      expect(calledId).toBe('sig-persisted-1');
      expect(calledBody.rules.entries[0].name).toBe('Edited Entry Block');
      // It must also carry the other persisted fields.
      expect(calledBody.description).toBe('hello world');
      expect(calledBody.category).toBe('RESEARCH');
    } finally {
      vi.useRealTimers();
    }
  });
});
