// @vitest-environment jsdom
//
// Regression for the same bug fixed on the Portfolio page (commit da3029f),
// applied to the Indicators page. A successful SAVE persists to the backend but
// NEVER refreshes the indicators LIST cache. The list query (staleTime 10s)
// then keeps the PRE-EDIT doc. On a REMOUNT (the user's "navigate away and
// back"), local ``indicators`` state starts empty and the merge effect
// (IndicatorsPage.jsx:321-353) adopts the stale cached doc — the dirty-
// preservation there only protects an in-progress LOCAL edit, not a completed
// save followed by a fresh mount. So the saved edit is silently reverted even
// though the backend has the new data.
//
// This test uses a SHARED QueryClient with staleTime:Infinity (a deterministic
// stand-in for the real 10s window). It selects a custom indicator, edits its
// doc, Saves, then unmounts + remounts with the same client and re-selects the
// custom indicator. Pre-fix the editor reverts to the old doc (FAIL); once the
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

// EditorPanel mock: surfaces the current ``doc`` and captures ``onDocChange``.
vi.mock('./EditorPanel', () => ({
  default: ({ doc, onDocChange }) => {
    capturedDocChange = onDocChange;
    return <div data-testid="ind-doc">{doc}</div>;
  },
}));

// IndicatorsList mock: select buttons keyed by id + the current selectedId.
vi.mock('./IndicatorsList', () => ({
  default: ({ indicators, selectedId, onSelect }) => (
    <div data-testid="indicators-list">
      <span data-testid="selected-id">{selectedId || ''}</span>
      {indicators.map((ind) => (
        <button
          key={ind.id}
          type="button"
          data-testid={`select-${ind.id}`}
          onClick={() => onSelect(ind.id)}
        >
          {ind.name}
        </button>
      ))}
    </div>
  ),
}));

vi.mock('./ParamsPanel', () => ({ default: () => <div /> }));
vi.mock('./IndicatorChart', () => ({ default: () => <div /> }));
vi.mock('../../components/ConfirmDialog', () => ({ default: () => null }));
vi.mock('../../api/indicators', () => ({
  resolveDefaultIndexInstrument: vi.fn(() => Promise.resolve({ ok: false, error: null })),
  computeIndicator: vi.fn(() => new Promise(() => {})),
}));

// A custom (non-readonly) backend indicator doc. ``definition`` is the opaque
// dict the page packs/unpacks. ``code`` has NO parseable params/series so the
// reconcilers keep params/seriesMap empty → the serialized payload is stable
// (no load-time dirtiness) and editing only ``doc`` is a clean single-field diff.
function freshDoc() {
  return {
    id: 'ind-1',
    type: 'indicator',
    name: 'My Indicator',
    locked: false,
    definition: {
      code: 'def compute(series):\n    return series',
      params: {},
      seriesMap: {},
      doc: 'original doc',
      ownPanel: false,
    },
  };
}

// MUTABLE backend store (kept faithful; with staleTime:Infinity the list is
// never refetched so survival across the remount is decided purely by the
// cache patch).
let store;

const mockListIndicators = vi.fn(() => Promise.resolve([...store]));
const mockUpdateIndicator = vi.fn((id, body) => {
  store = store.map((d) => (d.id === id ? { ...d, ...body, id } : d));
  return Promise.resolve(store.find((d) => d.id === id));
});
const mockCreateIndicator = vi.fn((doc) => {
  store = [...store, doc];
  return Promise.resolve(doc);
});
const mockArchiveIndicator = vi.fn(() => Promise.resolve(null));

vi.mock('../../api/persistence', () => ({
  CATEGORIES: ['RESEARCH', 'DEV', 'PROD', 'ARCHIVE'],
  listIndicators: (...a) => mockListIndicators(...a),
  createIndicator: (...a) => mockCreateIndicator(...a),
  updateIndicator: (...a) => mockUpdateIndicator(...a),
  archiveIndicator: (...a) => mockArchiveIndicator(...a),
  setIndicatorLocked: vi.fn(() => Promise.resolve({ ...freshDoc(), locked: true })),
  describePersistenceError: (err) => (err && err.message) || String(err),
  isLockedError: (err) => !!err && err.status === 423,
}));

import IndicatorsPage from './IndicatorsPage';
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
  mockListIndicators.mockClear();
  mockUpdateIndicator.mockClear();
  mockCreateIndicator.mockClear();
});

afterEach(() => {
  cleanup();
});

async function selectCustom() {
  await waitFor(() => {
    expect(screen.queryByTestId('select-ind-1')).not.toBeNull();
  });
  await act(async () => { fireEvent.click(screen.getByTestId('select-ind-1')); });
  await waitFor(() => {
    expect(screen.getByTestId('ind-doc').textContent).toBe('original doc');
  });
}

describe('<IndicatorsPage> — a saved edit survives re-opening the page', () => {
  it('select custom → edit doc → Save → remount shows the NEW doc (not the stale pre-edit doc)', async () => {
    const client = makeStaleClient();
    const first = renderWithClient(<IndicatorsPage />, { client });

    await selectCustom();

    // Turn autosave OFF so ONLY the manual Save path is under test.
    const autosaveCb = screen.getByRole('checkbox', { name: 'Auto save' });
    await act(async () => { fireEvent.click(autosaveCb); });

    // Edit the doc → dirty.
    await act(async () => { capturedDocChange('EDITED DOC'); });
    await waitFor(() => {
      expect(screen.getByTestId('ind-doc').textContent).toBe('EDITED DOC');
    });

    // Click Save → PUT persists the edited definition.
    const saveBtn = screen.getByRole('button', { name: 'Save' });
    await act(async () => { fireEvent.click(saveBtn); });
    await waitFor(() => { expect(mockUpdateIndicator).toHaveBeenCalled(); });
    const [, body] = mockUpdateIndicator.mock.calls[0];
    expect(body.definition.doc).toBe('EDITED DOC');
    // Flush the post-await cache patch inside handleBackendSave.
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    // Reopen: remount with the same (warm) client, then re-select the custom
    // indicator (selection resets to the first default on a fresh mount).
    first.unmount();
    renderWithClient(<IndicatorsPage />, { client });
    await waitFor(() => {
      expect(screen.queryByTestId('select-ind-1')).not.toBeNull();
    });
    await act(async () => { fireEvent.click(screen.getByTestId('select-ind-1')); });
    await waitFor(() => {
      expect(screen.getByTestId('ind-doc').textContent).toBe('EDITED DOC');
    });
  });
});
