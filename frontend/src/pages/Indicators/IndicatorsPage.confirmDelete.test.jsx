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

// Import AFTER the mocks so the page sees the stubs.
import IndicatorsPage from './IndicatorsPage';
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

const STORAGE_KEY = 'tcg.indicators.v1';

beforeEach(() => {
  try { localStorage.clear(); } catch { /* ignore */ }
  // Prime localStorage so the page hydrates with a deletable indicator.
  // Matches the shape storage.loadState expects — indicators[] at the root.
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    version: 1, // storage.SCHEMA_VERSION — hydrate rejects mismatches
    indicators: [USER_INDICATOR],
    defaultState: {},
  }));
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
  });
});
