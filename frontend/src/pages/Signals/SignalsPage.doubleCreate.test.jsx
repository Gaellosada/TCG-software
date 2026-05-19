// @vitest-environment jsdom
//
// Regression: clicking "+ New" must call createSignal exactly ONCE,
// even under React 18 StrictMode (which double-invokes state updater
// functions in dev to surface side effects).
//
// Root cause: createSignal was called inside the setSignals() updater
// callback.  StrictMode invokes updater functions twice, so the API
// call fired twice per click.  Fix: move the API call outside the
// updater, into the handler body.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';

// --- Mock all heavy sub-components so the test is unit-level -----------

let capturedOnAdd = null;

vi.mock('./SignalsList', () => ({
  default: ({ onAdd }) => {
    capturedOnAdd = onAdd;
    return (
      <button data-testid="add-signal-btn" type="button" onClick={onAdd}>
        + New
      </button>
    );
  },
}));
vi.mock('./BlockEditor', () => ({
  default: () => <div data-testid="block-editor-stub" />,
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

// The mock we care about: createSignal call count.
const mockCreateSignal = vi.fn(() => Promise.resolve({
  id: 'new-sig', name: 'Signal 1', category: 'RESEARCH',
  inputs: [], rules: {}, settings: {}, description: '',
}));
const mockListSignals = vi.fn(() => Promise.resolve([]));

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  createSignal: (...args) => mockCreateSignal(...args),
  listSignals: (...args) => mockListSignals(...args),
  updateSignal: vi.fn(() => Promise.resolve({})),
  archiveSignal: vi.fn(() => Promise.resolve(null)),
}));

import SignalsPage from './SignalsPage';

beforeEach(() => {
  capturedOnAdd = null;
  mockCreateSignal.mockClear();
  mockListSignals.mockClear();
  mockListSignals.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
});

describe('<SignalsPage> — double-create regression (StrictMode)', () => {
  it('calls createSignal exactly ONCE when "+ New" is clicked inside StrictMode', async () => {
    await act(async () => {
      render(
        <React.StrictMode>
          <SignalsPage />
        </React.StrictMode>,
      );
    });

    // Flush any initial fetch from useEffect (listSignals on mount).
    mockCreateSignal.mockClear();

    // Click "+ New" once.
    const addBtn = screen.getByTestId('add-signal-btn');
    await act(async () => {
      fireEvent.click(addBtn);
    });

    // createSignal must be called EXACTLY once — not twice.
    expect(mockCreateSignal).toHaveBeenCalledTimes(1);
  });
});
