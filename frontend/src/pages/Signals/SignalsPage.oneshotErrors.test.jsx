// @vitest-environment jsdom
//
// Wave 8 one-shot error surfacing:
//   Verify that handleAdd, handleConfirmDelete (archive), and
//   handleChangeItemCat all flip the CloudStatus indicator to 'error'
//   when the backend call rejects, and to 'saved' on success.

import React from 'react';
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react';

// --- Shared mock state -------------------------------------------------------

let capturedOnAdd = null;
let capturedOnDelete = null;
let capturedOnChangeItemCat = null;

vi.mock('./SignalsList', () => ({
  default: ({ signals, onAdd, onDelete, onChangeItemCat, onSelect }) => {
    capturedOnAdd = onAdd;
    capturedOnDelete = onDelete;
    capturedOnChangeItemCat = onChangeItemCat;
    return (
      <div data-testid="signals-list">
        <button data-testid="add-signal-btn" type="button" onClick={onAdd}>
          + New
        </button>
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
              data-testid={`delete-${s.id}`}
              type="button"
              onClick={() => onDelete(s.id)}
            >
              delete
            </button>
            <button
              data-testid={`cat-${s.id}`}
              type="button"
              onClick={() => onChangeItemCat(s.id, 'DEV')}
            >
              move to DEV
            </button>
          </div>
        ))}
      </div>
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

// ConfirmDialog mock — auto-confirms via the capturedOnConfirm ref.
let capturedOnConfirm = null;
vi.mock('../../components/ConfirmDialog', () => ({
  default: ({ open, onConfirm, onCancel }) => {
    capturedOnConfirm = onConfirm;
    if (!open) return null;
    return (
      <div data-testid="confirm-dialog">
        <button data-testid="confirm-btn" type="button" onClick={onConfirm}>Confirm</button>
      </div>
    );
  },
}));

// Persistence API mocks — controlled per test.
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
  capturedOnAdd = null;
  capturedOnDelete = null;
  capturedOnChangeItemCat = null;
  capturedOnConfirm = null;
  mockCreateSignal.mockReset();
  mockListSignals.mockReset();
  mockUpdateSignal.mockReset();
  mockArchiveSignal.mockReset();
  // Default: list returns one persisted signal.
  mockListSignals.mockResolvedValue([PERSISTED_DOC]);
  mockUpdateSignal.mockResolvedValue({ ...PERSISTED_DOC });
  mockArchiveSignal.mockResolvedValue(null);
  mockCreateSignal.mockResolvedValue({ ...PERSISTED_DOC, id: 'sig-new' });
});

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// handleAdd — backend failure
// ---------------------------------------------------------------------------
describe('<SignalsPage> one-shot error surfacing — handleAdd', () => {
  it('shows SaveStatus=error when createSignal rejects', async () => {
    mockCreateSignal.mockRejectedValue(new Error('network error'));

    await act(async () => {
      render(<SignalsPage />);
    });
    // Flush list-fetch.
    await act(async () => {});

    // Click "+ New".
    const addBtn = screen.getByTestId('add-signal-btn');
    await act(async () => {
      fireEvent.click(addBtn);
    });

    // The SaveStatus should show 'error'.
    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('error');
    });
  });

  it('shows SaveStatus=saved when createSignal resolves', async () => {
    mockCreateSignal.mockResolvedValue({ ...PERSISTED_DOC, id: 'sig-ok' });

    await act(async () => {
      render(<SignalsPage />);
    });
    await act(async () => {});

    const addBtn = screen.getByTestId('add-signal-btn');
    await act(async () => {
      fireEvent.click(addBtn);
    });

    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('saved');
    });
  });
});

// ---------------------------------------------------------------------------
// handleConfirmDelete (archive) — backend failure
// ---------------------------------------------------------------------------
describe('<SignalsPage> one-shot error surfacing — handleConfirmDelete (archive)', () => {
  it('shows SaveStatus=error when archiveSignal rejects', async () => {
    mockArchiveSignal.mockRejectedValue(new Error('network error'));

    await act(async () => {
      render(<SignalsPage />);
    });
    // Wait for the persisted list to render.
    await waitFor(() => {
      expect(screen.queryByTestId('select-sig-1')).not.toBeNull();
    });

    // Select the signal first so SaveStatus is guaranteed to be rendered.
    await act(async () => {
      fireEvent.click(screen.getByTestId('select-sig-1'));
    });
    await act(async () => {});

    // Click delete on the signal — opens confirmation dialog.
    await act(async () => {
      fireEvent.click(screen.getByTestId('delete-sig-1'));
    });

    // Confirm the deletion.
    await act(async () => {
      fireEvent.click(screen.getByTestId('confirm-btn'));
    });

    // Expect error status — signal was selected so SaveStatus is present.
    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('error');
    });
  });
});

// ---------------------------------------------------------------------------
// handleChangeItemCat — backend failure
// ---------------------------------------------------------------------------
describe('<SignalsPage> one-shot error surfacing — handleChangeItemCat', () => {
  it('shows SaveStatus=error when updateSignal rejects on category change', async () => {
    mockUpdateSignal.mockRejectedValue(new Error('network error'));

    await act(async () => {
      render(<SignalsPage />);
    });
    await waitFor(() => {
      expect(screen.queryByTestId('select-sig-1')).not.toBeNull();
    });

    // Select the signal so SaveStatus is rendered.
    await act(async () => {
      fireEvent.click(screen.getByTestId('select-sig-1'));
    });
    await act(async () => {});

    // Trigger category change.
    await act(async () => {
      fireEvent.click(screen.getByTestId('cat-sig-1'));
    });

    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('error');
    });
  });

  it('shows SaveStatus=saved when updateSignal resolves on category change', async () => {
    mockUpdateSignal.mockResolvedValue({ ...PERSISTED_DOC, category: 'DEV' });

    await act(async () => {
      render(<SignalsPage />);
    });
    await waitFor(() => {
      expect(screen.queryByTestId('select-sig-1')).not.toBeNull();
    });

    // Select the signal.
    await act(async () => {
      fireEvent.click(screen.getByTestId('select-sig-1'));
    });
    await act(async () => {});

    // Trigger category change.
    await act(async () => {
      fireEvent.click(screen.getByTestId('cat-sig-1'));
    });

    await waitFor(() => {
      const el = screen.queryByTestId('save-status');
      expect(el).not.toBeNull();
      expect(el.dataset.status).toBe('saved');
    });
  });
});
