// @vitest-environment jsdom
//
// Minimal SignalsPage tests covering the Statistics-mount wiring and
// the M1 regression (lastResult cleared on signal switch).
//
// The iter4 hoist removed end-to-end tests asserting that <Statistics>
// is page-level (not inside ResultsCard). These tests restore the
// minimum guarantee: the ``signal-statistics`` testid only appears when
// ``buildSignalStatsInputs`` returns a non-null payload — the same gate
// the page applies in production.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';

// Capture callbacks so tests can drive the page imperatively.
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
vi.mock('../../components/Statistics', () => ({
  default: () => <div data-testid="statistics-stub" />,
}));
vi.mock('../../components/TradeLog', () => ({
  default: ({ entryDescriptions, exitDescriptions }) => (
    <div
      data-testid="trade-log-stub"
      data-entry-desc-keys={Object.keys(entryDescriptions || {}).join(',')}
      data-exit-desc-keys={Object.keys(exitDescriptions || {}).join(',')}
    />
  ),
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

// Mock persistence API so tests don't attempt real HTTP calls.
vi.mock('../../api/persistence', () => ({
  listSignals: vi.fn(() => Promise.resolve([])),
  createSignal: vi.fn(() => Promise.resolve({})),
  updateSignal: vi.fn(() => Promise.resolve({})),
  archiveSignal: vi.fn(() => Promise.resolve(null)),
}));

// Allow the run gate to pass so M1 tests can populate lastResult.
vi.mock('./runGate', () => ({
  computeRunGate: () => ({ runDisabledReason: null, missingIds: [] }),
}));

// Bypass requestBuilder so it returns a valid body for the fake run.
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

// Two signals for the M1 switch test.
const SIG_A = {
  id: 'sig-a', name: 'Signal A',
  inputs: [],
  rules: { entries: [], exits: [] },
  settings: { dont_repeat: true },
  doc: '',
};
const SIG_B = {
  id: 'sig-b', name: 'Signal B',
  inputs: [],
  rules: { entries: [], exits: [] },
  settings: { dont_repeat: true },
  doc: '',
};

// Mutable storage factory so individual tests can control loadState return.
const mockLoadState = vi.fn(() => ({ signals: [] }));
vi.mock('./storage', async () => {
  const actual = await vi.importActual('./storage');
  return {
    ...actual,
    loadState: (...args) => mockLoadState(...args),
    saveState: vi.fn(),
  };
});

import SignalsPage from './SignalsPage';

afterEach(() => {
  cleanup();
  capturedOnRun = null;
  capturedOnSelect = null;
  mockComputeSignal.mockReset();
  mockLoadState.mockReset();
  mockLoadState.mockReturnValue({ signals: [] });
});

describe('<SignalsPage> — Statistics wiring', () => {
  it('does NOT render the signal-statistics panel when lastResult is null', async () => {
    await act(async () => {
      render(<SignalsPage />);
    });
    // ResultsView mounts (always), but the page-level Statistics panel
    // only appears once buildSignalStatsInputs returns non-null — which
    // requires a successful run. Initial state must be empty.
    expect(screen.queryByTestId('signal-statistics')).toBeNull();
    expect(screen.getByTestId('results-view-stub')).toBeTruthy();
  });

  it('does NOT mount the TradeLog when lastResult is null', async () => {
    await act(async () => {
      render(<SignalsPage />);
    });
    expect(screen.queryByTestId('trade-log-stub')).toBeNull();
  });
});

describe('<SignalsPage> — M1 regression: lastResult cleared on signal switch', () => {
  it('hides TradeLog after switching to a different signal following a completed run', async () => {
    // Load two signals so there is a second signal to switch to.
    mockLoadState.mockReturnValue({ signals: [SIG_A, SIG_B] });

    // computeSignal resolves with a minimal result containing trades.
    const fakeResult = {
      timestamps: [1000, 2000, 3000],
      positions: [],
      events: [],
      trades: [
        {
          input_id: 'X', entry_block_id: 'e1', entry_block_name: 'E1',
          exit_block_id: null, exit_block_name: null,
          open_bar: 0, close_bar: null, direction: 'long', signed_weight: 0.5,
        },
      ],
    };
    mockComputeSignal.mockResolvedValue(fakeResult);

    await act(async () => {
      render(<SignalsPage />);
    });

    // Before any run: TradeLog must not be present.
    expect(screen.queryByTestId('trade-log-stub')).toBeNull();

    // Trigger the run via the captured onRun. The runGate mock allows
    // the run (runDisabledReason: null), and requestBuilder mock returns
    // a valid body, so computeSignal will be called and lastResult set.
    expect(capturedOnRun).not.toBeNull();
    await act(async () => {
      await capturedOnRun();
    });

    // After the run: TradeLog must now be visible (lastResult populated).
    expect(screen.getByTestId('trade-log-stub')).toBeTruthy();

    // Switch to SIG_B — the M1 useEffect must clear lastResult.
    expect(capturedOnSelect).not.toBeNull();
    await act(async () => {
      capturedOnSelect('sig-b');
    });

    // TradeLog must be unmounted — no stale results from the previous signal.
    expect(screen.queryByTestId('trade-log-stub')).toBeNull();
  });
});

describe('<SignalsPage> — entryDescriptions + exitDescriptions wiring', () => {
  it('threads both description maps to TradeLog after a completed run', async () => {
    const SIG_WITH_BLOCKS = {
      id: 'sig-c', name: 'Signal C',
      inputs: [],
      rules: {
        entries: [{ id: 'entry-1', description: 'RSI < 30', conditions: [] }],
        exits: [{ id: 'exit-1', description: 'RSI > 70', conditions: [] }],
      },
      settings: { dont_repeat: true },
      doc: '',
    };
    mockLoadState.mockReturnValue({ signals: [SIG_WITH_BLOCKS] });

    const fakeResult = {
      timestamps: [1000, 2000],
      positions: [],
      events: [],
      trades: [{
        input_id: 'X', entry_block_id: 'entry-1', entry_block_name: 'My Entry',
        exit_block_id: 'exit-1', exit_block_name: 'My Exit',
        open_bar: 0, close_bar: 1, direction: 'long', signed_weight: 0.5,
      }],
    };
    mockComputeSignal.mockResolvedValue(fakeResult);

    await act(async () => { render(<SignalsPage />); });

    await act(async () => { await capturedOnRun(); });

    const stub = screen.getByTestId('trade-log-stub');
    expect(stub.getAttribute('data-entry-desc-keys')).toBe('entry-1');
    expect(stub.getAttribute('data-exit-desc-keys')).toBe('exit-1');
  });
});

describe('<SignalsPage> — legacy v5 hydration (reset blocks)', () => {
  // T19: a v5 payload that lacks rules.resets must hydrate cleanly with
  // rules.resets defaulting to [] — no crash, no missing-key errors.
  it('hydrates a legacy v5 signal without rules.resets, defaulting to []', async () => {
    const legacy = {
      id: 'legacy-sig',
      name: 'Legacy Sig',
      inputs: [],
      rules: { entries: [], exits: [] },
      settings: { dont_repeat: true },
      doc: '',
    };
    mockLoadState.mockReturnValue({ signals: [legacy] });
    await act(async () => { render(<SignalsPage />); });
    // ResultsView stub renders unconditionally — its presence proves the
    // page hydrated without throwing on the missing rules.resets field.
    expect(screen.getByTestId('results-view-stub')).toBeTruthy();
  });
});
