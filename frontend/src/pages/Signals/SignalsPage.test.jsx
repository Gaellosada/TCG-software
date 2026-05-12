// @vitest-environment jsdom
//
// Minimal SignalsPage tests covering the Statistics-mount wiring.
//
// The iter4 hoist removed end-to-end tests asserting that <Statistics>
// is page-level (not inside ResultsCard). These tests restore the
// minimum guarantee: the ``signal-statistics`` testid only appears when
// ``buildSignalStatsInputs`` returns a non-null payload — the same gate
// the page applies in production.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';

// Heavy children get stubbed — we are only testing the Statistics
// conditional mount, not the rest of the page.
vi.mock('./SignalsList', () => ({
  default: () => <div data-testid="signals-list-stub" />,
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
vi.mock('../../api/statistics', () => ({
  fetchStatistics: vi.fn(),
}));
vi.mock('./hydrateIndicators', () => ({
  hydrateAvailableIndicators: () => [],
}));
vi.mock('../../api/signals', () => ({
  computeSignal: vi.fn(),
}));
// Storage starts empty by default for an isolated test environment.
vi.mock('./storage', async () => {
  const actual = await vi.importActual('./storage');
  return {
    ...actual,
    loadState: () => ({ signals: [] }),
    saveState: vi.fn(),
  };
});

import SignalsPage from './SignalsPage';

afterEach(() => {
  cleanup();
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
});
