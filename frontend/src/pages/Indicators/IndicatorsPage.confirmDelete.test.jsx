// @vitest-environment jsdom
//
// iter-4: IndicatorsPage used to delete user indicators through a
// synchronous window.confirm. It now routes through the shared
// ConfirmDialog. This file asserts:
//   - clicking Delete on a user indicator opens the ConfirmDialog
//   - Escape cancels (indicator stays)
//   - Enter confirms (indicator is removed)
//   - window.confirm is never invoked
//
// IndicatorsPage pulls in Plotly (via Chart) and the backend API; both
// are mocked so this file stays a pure unit render.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';

// Stub the shared Chart — Plotly touches ``self`` which blows up in jsdom.
vi.mock('../../components/Chart', () => {
  function ChartStub() { return null; }
  return { default: ChartStub };
});

// Stub the only backend call the page makes on mount. The envelope
// shape must match the real api contract: { ok, data } | { ok:false, error }.
vi.mock('../../api/indicators', () => ({
  resolveDefaultIndexInstrument: vi.fn(async () => ({
    ok: true,
    data: { collection: 'equity_etf', instrument_id: 'SPY' },
  })),
}));

// Stub persistence API — the page now fetches custom indicators from
// backend on mount and archives via DELETE on confirm-delete.
vi.mock('../../api/persistence', () => ({
  listIndicators: vi.fn(async () => []),
  createIndicator: vi.fn(async (p) => ({ ...p, type: 'indicator', created_at: '', updated_at: '', deleted: false })),
  updateIndicator: vi.fn(async (_id, p) => p),
  archiveIndicator: vi.fn(async () => null),
  describePersistenceError: vi.fn((err) => err?.message || 'Unknown error'),
}));

// Import AFTER the mocks so the page sees the stubs.
import IndicatorsPage from './IndicatorsPage';
import { listIndicators, archiveIndicator } from '../../api/persistence';
import { AUTOSAVE_KEY } from './storageKeys';

// Fixture: a user indicator with readonly:false — defaults are readonly
// so they cannot be deleted; we must seed a user-created one.
const USER_INDICATOR = {
  id: 'user-ind-1',
  name: 'My Test Indicator',
  code: 'def compute(series):\n    return series["price"]\n',
  doc: '',
  params: {},
  seriesMap: {},
  ownPanel: false,
};

beforeEach(() => {
  try { localStorage.clear(); } catch { /* ignore */ }
  // Prime the backend mock so the page hydrates with a deletable indicator.
  // The page now fetches custom indicators from listIndicators() on mount.
  listIndicators.mockResolvedValue([{
    id: USER_INDICATOR.id,
    type: 'indicator',
    name: USER_INDICATOR.name,
    definition: {
      code: USER_INDICATOR.code,
      doc: USER_INDICATOR.doc,
      params: USER_INDICATOR.params,
      seriesMap: USER_INDICATOR.seriesMap,
      ownPanel: USER_INDICATOR.ownPanel,
    },
    created_at: '',
    updated_at: '',
    deleted: false,
  }]);
  archiveIndicator.mockResolvedValue(null);
  // Disable autosave so the test doesn't race with side-effects.
  localStorage.setItem(AUTOSAVE_KEY, 'false');
});

afterEach(() => {
  cleanup();
  try { localStorage.clear(); } catch { /* ignore */ }
  vi.restoreAllMocks();
});

/**
 * The CUSTOM section is collapsed by default (bullet #6 of the v4
 * refactor). User indicator rows — and their delete buttons — only
 * render when the section is expanded. Each test below expands CUSTOM
 * by clicking its header the way a user would, rather than pre-priming
 * localStorage (which bypasses the UI contract).
 */
function expandCustomSection() {
  const header = screen.getByTestId('category-custom');
  if (header.getAttribute('data-collapsed') === 'true') {
    fireEvent.click(header);
  }
}

describe('<IndicatorsPage> delete confirmation flow', () => {
  it('clicking Delete on a user indicator opens ConfirmDialog (not window.confirm)', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm');
    await act(async () => {
      render(<IndicatorsPage />);
    });
    expandCustomSection();
    const deleteBtn = screen.getByLabelText('Delete My Test Indicator');
    fireEvent.click(deleteBtn);
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('Escape cancels and keeps the indicator', async () => {
    await act(async () => {
      render(<IndicatorsPage />);
    });
    expandCustomSection();
    const deleteBtn = screen.getByLabelText('Delete My Test Indicator');
    fireEvent.click(deleteBtn);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
    // Indicator still in the list.
    expect(screen.getByLabelText('Delete My Test Indicator')).toBeDefined();
  });

  it('Enter confirms and removes the user indicator', async () => {
    await act(async () => {
      render(<IndicatorsPage />);
    });
    expandCustomSection();
    const deleteBtn = screen.getByLabelText('Delete My Test Indicator');
    fireEvent.click(deleteBtn);
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
    fireEvent.keyDown(document, { key: 'Enter' });
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
    // The delete-button for the user indicator should no longer exist.
    expect(screen.queryByLabelText('Delete My Test Indicator')).toBeNull();
    // T1: verify the backend archive call was actually made.
    expect(archiveIndicator).toHaveBeenCalledWith('user-ind-1');
  });

  // T2: When archiveIndicator rejects, the indicator should be rolled back
  // into the list and the error surfaced.
  it('rolls back the indicator when archiveIndicator rejects', async () => {
    archiveIndicator.mockRejectedValueOnce(new Error('network error'));
    await act(async () => {
      render(<IndicatorsPage />);
    });
    expandCustomSection();
    fireEvent.click(screen.getByLabelText('Delete My Test Indicator'));
    await act(async () => {
      fireEvent.keyDown(document, { key: 'Enter' });
    });
    // Wait for the rejection to settle.
    await act(async () => {});
    expandCustomSection();
    // Indicator should be restored in the DOM.
    expect(screen.getByLabelText('Delete My Test Indicator')).toBeDefined();
  });

  // T3: When createIndicator rejects, the optimistically added indicator
  // should be removed from the list.
  it('rolls back the indicator when createIndicator rejects', async () => {
    const { createIndicator } = await import('../../api/persistence');
    createIndicator.mockRejectedValueOnce(new Error('server error'));
    await act(async () => {
      render(<IndicatorsPage />);
    });
    // Count indicators before add.
    expandCustomSection();
    const beforeCount = screen.queryAllByLabelText(/^Delete /).length;
    // Click the "+" button to add a new indicator.
    const addBtn = screen.getByLabelText('New indicator');
    await act(async () => {
      fireEvent.click(addBtn);
    });
    // Wait for the rejection to settle.
    await act(async () => {});
    expandCustomSection();
    // The optimistically added indicator should have been removed.
    const afterCount = screen.queryAllByLabelText(/^Delete /).length;
    expect(afterCount).toBe(beforeCount);
  });
});
