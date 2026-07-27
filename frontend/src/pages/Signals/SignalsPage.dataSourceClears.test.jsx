// @vitest-environment jsdom
//
// Flipping the per-run data source must CLEAR the previously computed result.
//
// The whole point of the v1/v2 selector is "run it on v1, run it on v2, compare".
// If the previous source's chart / statistics / trade log stay on screen after a
// flip, a user can read the unchanged chart as "v2 gives the same answer" without
// ever having pressed Run — a silent false "no difference". The data source is an
// input that invalidates a computed result exactly like switching signals does,
// so it must blank the result the same way.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';

vi.mock('./SignalsList', () => ({
  default: ({ signals, onSelect }) => (
    <div data-testid="signals-list">
      {signals.map((s) => (
        <button key={s.id} data-testid={`select-${s.id}`} type="button" onClick={() => onSelect(s.id)}>
          {s.name}
        </button>
      ))}
    </div>
  ),
}));
vi.mock('./BlockEditor', () => ({ default: () => <div data-testid="block-editor-stub" /> }));
vi.mock('./InputsPanel', () => ({ default: () => <div data-testid="inputs-panel-stub" /> }));
vi.mock('../../components/Statistics', () => ({ default: () => <div data-testid="statistics-stub" /> }));
vi.mock('../../components/TradeLog', () => ({ default: () => <div data-testid="trade-log-stub" /> }));

// ParamsPanel stub: exposes Run + a data-source flip, exactly the two controls
// this behaviour is about.
vi.mock('./ParamsPanel', () => ({
  default: ({ onRun, dataSource, onDataSourceChange }) => (
    <div data-testid="params-panel-stub">
      <span data-testid="current-source">{dataSource}</span>
      <button data-testid="run-btn" type="button" onClick={onRun}>Run</button>
      <button data-testid="flip-source-btn" type="button" onClick={() => onDataSourceChange('v2')}>
        to v2
      </button>
    </div>
  ),
}));

// ResultsView stub: renders a marker ONLY when a result is present, so "the
// previous run is still on screen" is directly observable.
vi.mock('./ResultsView', () => ({
  default: ({ result }) => (
    <div data-testid="results-view-stub">
      {result ? <div data-testid="displayed-result">{result.marker}</div> : null}
    </div>
  ),
}));

vi.mock('./hydrateIndicators', () => ({
  hydrateAvailableIndicators: () => Promise.resolve([]),
}));

const mockComputeSignal = vi.fn();
vi.mock('../../api/signals', () => ({
  computeSignal: (...args) => mockComputeSignal(...args),
  collectIndicatorIds: () => new Set(),
}));
vi.mock('./runGate', () => ({
  computeRunGate: () => ({ runDisabledReason: '', missingIds: [] }),
}));
vi.mock('./requestBuilder', () => ({
  buildComputeRequestBody: (_signal, _inds, _costs, dataSource) => ({
    body: {
      spec: {}, indicators: [],
      ...(dataSource === 'v2' ? { data_source: 'v2' } : {}),
    },
    missing: [],
  }),
}));
vi.mock('./storage', () => ({
  loadState: () => ({ signals: [] }),
  saveState: vi.fn(),
  emptyRules: () => ({ entries: [], exits: [], resets: [] }),
  defaultSettings: () => ({ dont_repeat: true }),
}));
vi.mock('../../components/ConfirmDialog', () => ({ default: () => null }));

const mockListSignals = vi.fn();
vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  createSignal: vi.fn(),
  listSignals: (...args) => mockListSignals(...args),
  updateSignal: vi.fn(() => Promise.resolve(null)),
  archiveSignal: vi.fn(() => Promise.resolve(null)),
  describePersistenceError: (err) => (err && err.message) || String(err),
}));

import SignalsPage from './SignalsPage';

const PERSISTED_DOC = {
  id: 'sig-1',
  type: 'signal',
  name: 'Saved Signal',
  category: 'RESEARCH',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  inputs: [],
  rules: { entries: [], exits: [], resets: [] },
  settings: { dont_repeat: true },
  description: '',
};

beforeEach(() => {
  mockComputeSignal.mockReset();
  mockComputeSignal.mockResolvedValue({ marker: 'V1-RESULT', timestamps: [], positions: [], trades: [] });
  mockListSignals.mockReset();
  mockListSignals.mockResolvedValue([PERSISTED_DOC]);
});

afterEach(() => { cleanup(); });

describe('SignalsPage — data source flip invalidates the displayed result', () => {
  it('clears the previously computed result when the data source changes', async () => {
    render(<SignalsPage />);
    fireEvent.click(await screen.findByTestId('select-sig-1'));

    // Run on v1 → a result is displayed.
    fireEvent.click(screen.getByTestId('run-btn'));
    await waitFor(() => expect(screen.getByTestId('displayed-result').textContent).toBe('V1-RESULT'));

    // Flip to v2 WITHOUT re-running: the v1 result must not stay on screen,
    // otherwise the unchanged chart reads as "v2 gives the same answer".
    fireEvent.click(screen.getByTestId('flip-source-btn'));
    await waitFor(() => expect(screen.getByTestId('current-source').textContent).toBe('v2'));
    expect(screen.queryByTestId('displayed-result')).toBeNull();
    expect(screen.queryByTestId('signal-statistics')).toBeNull();
  });
});
