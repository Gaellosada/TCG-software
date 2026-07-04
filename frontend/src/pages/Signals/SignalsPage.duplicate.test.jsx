// @vitest-environment jsdom
//
// Page-level integration: clicking the duplicate (⧉) action on a signal row
// exercises the REAL handleDuplicate + storage.duplicateSignal path and:
//   1. creates the copy on the backend EXACTLY ONCE, even under React 18
//      StrictMode (which double-invokes updater functions in dev to surface
//      side effects — the same trap the "+ New" double-create guard closes);
//   2. adds a new "<name> (copy)" signal to the list that is UNLOCKED
//      (locked:false → editable, not routed through the locked read-only view).
//
// duplicateSignal is used FOR REAL (via importActual) so the (copy) name +
// unlocked flag come from production code, not a stub.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act, within } from '@testing-library/react';

// --- SignalsList stub: renders the real ``signals`` prop as rows (name +
//     lock state) and a per-row duplicate button wired to onDuplicate, so we
//     can click ⧉ and then assert the copy appears in the re-rendered list.
vi.mock('./SignalsList', () => ({
  default: ({ signals, onDuplicate }) => (
    <div>
      {signals.map((s) => (
        <div key={s.id} data-testid={`row-${s.id}`} data-locked={String(!!s.locked)}>
          <span data-testid={`name-${s.id}`}>{s.name}</span>
          <button
            type="button"
            data-testid={`dup-${s.id}`}
            title="Duplicate"
            onClick={() => onDuplicate(s.id)}
          >
            ⧉
          </button>
        </div>
      ))}
    </div>
  ),
}));
vi.mock('./BlockEditor', () => ({ default: () => <div data-testid="block-editor-stub" /> }));
vi.mock('./ParamsPanel', () => ({ default: () => <div data-testid="params-panel-stub" /> }));
vi.mock('./InputsPanel', () => ({ default: () => <div data-testid="inputs-panel-stub" /> }));
vi.mock('./ResultsView', () => ({ default: () => <div data-testid="results-view-stub" /> }));
vi.mock('../../components/Statistics', () => ({ default: () => <div data-testid="statistics-stub" /> }));
vi.mock('../../components/TradeLog', () => ({ default: () => <div data-testid="trade-log-stub" /> }));
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

// Real duplicateSignal (+ emptyRules/defaultSettings) — the storage path is
// the thing under test; only override nothing.
vi.mock('./storage', async (importActual) => {
  const actual = await importActual();
  return { ...actual };
});

// Seed one source signal via the list query hook so it lands in page state.
const SOURCE = {
  id: 'src-1',
  name: 'Signal 1',
  inputs: [],
  rules: { entries: [], exits: [], resets: [] },
  settings: { dont_repeat: true },
  description: '',
  locked: false,
};
// STABLE references — SignalsPage's mount effect depends on
// ``signalsQuery.data`` and its callbacks on ``invalidate``; returning fresh
// objects each render would loop forever (re-render -> new array -> effect ->
// setState -> ...).
const SOURCE_LIST = [SOURCE];
const SIGNALS_QUERY = { data: SOURCE_LIST, isPending: false, fetchStatus: 'idle', error: null };
const INVALIDATE = { signals: vi.fn() };
vi.mock('../../hooks/persistenceQueries', () => ({
  useSignalsList: () => SIGNALS_QUERY,
  useInvalidatePersistence: () => INVALIDATE,
}));

const mockCreateSignal = vi.fn((doc) => Promise.resolve({ ...doc, category: 'RESEARCH' }));

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  createSignal: (...args) => mockCreateSignal(...args),
  updateSignal: vi.fn(() => Promise.resolve({})),
  archiveSignal: vi.fn(() => Promise.resolve(null)),
  setSignalLocked: vi.fn(() => Promise.resolve({})),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: () => false,
}));

import SignalsPage from './SignalsPage';

beforeEach(() => {
  mockCreateSignal.mockClear();
});

afterEach(() => {
  cleanup();
});

describe('<SignalsPage> — duplicate flow (StrictMode)', () => {
  it('duplicates a row into a new unlocked "(copy)" signal, creating it exactly ONCE', async () => {
    await act(async () => {
      render(
        <React.StrictMode>
          <SignalsPage />
        </React.StrictMode>,
      );
    });
    mockCreateSignal.mockClear(); // ignore any mount-time calls

    // Click ⧉ on the source row exactly once.
    await act(async () => {
      fireEvent.click(screen.getByTestId('dup-src-1'));
    });

    // Guard against StrictMode double-invoke: create fires exactly once.
    expect(mockCreateSignal).toHaveBeenCalledTimes(1);

    // A new "Signal 1 (copy)" row is now in the list.
    const copyName = screen.getByText('Signal 1 (copy)');
    expect(copyName).toBeTruthy();

    // The created payload carries the (copy) name; the copy is UNLOCKED
    // (editable) — its list row is not marked locked.
    const createArg = mockCreateSignal.mock.calls[0][0];
    expect(createArg.name).toBe('Signal 1 (copy)');
    expect(createArg.id).not.toBe('src-1');

    const copyRow = copyName.closest('[data-testid^="row-"]');
    expect(copyRow.getAttribute('data-locked')).toBe('false');

    // Original row is untouched (still present, still one copy).
    expect(screen.getByTestId('name-src-1').textContent).toBe('Signal 1');
    expect(screen.getAllByText('Signal 1 (copy)')).toHaveLength(1);
  });
});
