// @vitest-environment jsdom
//
// Regression for the same bug fixed on the Portfolio page (commit da3029f):
// a successful SAVE persists to the backend but NEVER refreshes the signals
// LIST cache. The list query (staleTime 10s) then keeps the PRE-EDIT doc, and
// SignalsPage re-seeds its local ``signals`` state from ``signalsQuery.data``
// via ``hydrateFromPersisted`` on (re)mount (effect at SignalsPage.jsx:112-122).
// So navigating away and back within the stale window re-hydrates the stale
// cached doc and silently reverts the saved edit — even though the backend has
// the new data.
//
// This test uses a SHARED QueryClient with staleTime:Infinity (a deterministic
// stand-in for the real 10s window: a remount serves the warm cache instead of
// refetching). It edits a signal, Saves, then unmounts + remounts with the same
// client. Pre-fix the remounted editor reverts to the old doc (FAIL); once the
// save patches the list cache the edit SURVIVES the reopen (PASS).

import React from 'react';
import {
  describe, it, expect, vi, afterEach, beforeEach,
} from 'vitest';
import {
  screen, fireEvent, cleanup, act, waitFor,
} from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import { renderWithClient } from '../../test/queryWrapper';

let capturedDocChange = null;

// BlockEditor mock: surfaces the current ``doc`` and captures ``onDocChange``
// so the test can drive an edit and read back what the editor holds.
vi.mock('./BlockEditor', () => ({
  default: ({ doc, onDocChange }) => {
    capturedDocChange = onDocChange;
    return <div data-testid="doc-value">{doc}</div>;
  },
}));

// SignalsList mock: select buttons + the current selectedId.
vi.mock('./SignalsList', () => ({
  default: ({ signals, selectedId, onSelect }) => (
    <div data-testid="signals-list">
      <span data-testid="selected-id">{selectedId || ''}</span>
      {signals.map((s) => (
        <button
          key={s.id}
          type="button"
          data-testid={`select-${s.id}`}
          onClick={() => onSelect(s.id)}
        >
          {s.name}
        </button>
      ))}
    </div>
  ),
}));

vi.mock('./InputsPanel', () => ({ default: () => <div /> }));
vi.mock('./ParamsPanel', () => ({ default: () => <div /> }));
vi.mock('./ResultsView', () => ({ default: () => <div /> }));
vi.mock('../../components/Statistics', () => ({ default: () => <div /> }));
vi.mock('../../components/TradeLog', () => ({ default: () => <div /> }));
vi.mock('../../components/ConfirmDialog', () => ({ default: () => null }));
vi.mock('../../api/signals', () => ({
  computeSignal: vi.fn(() => new Promise(() => {})),
}));
vi.mock('./hydrateIndicators', () => ({
  hydrateAvailableIndicators: vi.fn(() => Promise.resolve([])),
}));

function freshDoc() {
  return {
    id: 'sig-1',
    type: 'signal',
    name: 'My Signal',
    category: 'RESEARCH',
    inputs: [],
    rules: {},
    settings: {},
    description: 'original doc',
    locked: false,
  };
}

// MUTABLE backend store — a real backend would persist PUTs. (With
// staleTime:Infinity the list is never refetched, so what survives the remount
// is decided purely by whether the SAVE patched the cache — this store update
// just keeps the mock faithful.)
let store;

const mockListSignals = vi.fn((cat) => Promise.resolve(store.filter((s) => s.category === cat)));
const mockUpdateSignal = vi.fn((id, body) => {
  store = store.map((s) => (s.id === id ? { ...s, ...body, id } : s));
  return Promise.resolve(store.find((s) => s.id === id));
});
const mockCreateSignal = vi.fn((doc) => {
  store = [...store, doc];
  return Promise.resolve(doc);
});
const mockArchiveSignal = vi.fn(() => Promise.resolve(null));

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listSignals: (...a) => mockListSignals(...a),
  createSignal: (...a) => mockCreateSignal(...a),
  updateSignal: (...a) => mockUpdateSignal(...a),
  archiveSignal: (...a) => mockArchiveSignal(...a),
  setSignalLocked: vi.fn(() => Promise.resolve({ ...freshDoc(), locked: true })),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: (err) => !!err && err.status === 423,
}));

import SignalsPage from './SignalsPage';
import { queryKeys } from '../../queryKeys';

function makeStaleClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity, staleTime: Infinity },
    },
  });
}

beforeEach(() => {
  capturedDocChange = null;
  store = [freshDoc()];
  mockListSignals.mockClear();
  mockUpdateSignal.mockClear();
  mockCreateSignal.mockClear();
});

afterEach(() => {
  cleanup();
});

async function waitForLoadedDoc() {
  await waitFor(() => {
    expect(screen.queryByTestId('doc-value')).not.toBeNull();
    expect(screen.getByTestId('doc-value').textContent).toBe('original doc');
  });
}

describe('<SignalsPage> — a saved edit survives re-opening the page', () => {
  it('edit doc → Save → remount shows the NEW doc (not the stale pre-edit doc)', async () => {
    const client = makeStaleClient();
    const first = renderWithClient(<SignalsPage />, { client });

    // Signal auto-selects (first in list) and the editor shows the original doc.
    await waitForLoadedDoc();

    // Turn autosave OFF so ONLY the manual Save path is under test.
    const autosaveCb = screen.getByRole('checkbox', { name: 'Auto save' });
    await act(async () => { fireEvent.click(autosaveCb); });

    // Edit the doc → dirty.
    await act(async () => { capturedDocChange('EDITED DOC'); });
    await waitFor(() => {
      expect(screen.getByTestId('doc-value').textContent).toBe('EDITED DOC');
    });

    // Click Save → PUT persists the edited doc.
    const saveBtn = screen.getByRole('button', { name: 'Save' });
    await act(async () => { fireEvent.click(saveBtn); });
    await waitFor(() => { expect(mockUpdateSignal).toHaveBeenCalled(); });
    const [, body] = mockUpdateSignal.mock.calls[0];
    expect(body.description).toBe('EDITED DOC'); // doc → description on the wire
    // Flush the post-await cache patch inside handleBackendSave.
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    // The user "navigates away and returns" → the page remounts with the same
    // (warm) client. If the save refreshed the list cache the remounted editor
    // shows the SAVED doc; if it kept the stale pre-edit doc it reverts (bug).
    first.unmount();
    renderWithClient(<SignalsPage />, { client });
    await waitFor(() => {
      expect(screen.getByTestId('doc-value').textContent).toBe('EDITED DOC');
    });
  });
});
