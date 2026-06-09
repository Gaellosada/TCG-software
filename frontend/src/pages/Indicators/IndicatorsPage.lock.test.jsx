// @vitest-environment jsdom
//
// Tests for the lock feature on the Indicators page:
//   - lock toggle wiring calls setIndicatorLocked
//   - locked row disables edit + delete controls
//   - read-only editor banner when currently-open indicator is locked
//   - built-in readonly indicators show no lock toggle

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';

// Stub Plotly.
vi.mock('../../components/Chart', () => {
  function ChartStub() { return null; }
  return { default: ChartStub };
});

// Stub indicators API.
vi.mock('../../api/indicators', () => ({
  resolveDefaultIndexInstrument: vi.fn(async () => ({
    ok: true,
    data: { collection: 'equity_etf', instrument_id: 'SPY' },
  })),
}));

// Build a full IndicatorOut-shaped doc so the mocked lock endpoint matches the
// real backend response (id/name/definition/locked/timestamps/type/deleted),
// not just { locked }. The real setIndicatorLocked returns the updated doc.
function indicatorOut(over = {}) {
  return {
    id: over.id ?? 'user-ind-1',
    name: over.name ?? 'My Unlocked Indicator',
    definition: over.definition ?? {
      code: 'def compute(series):\n    return series["price"]\n',
      doc: '',
      params: {},
      seriesMap: {},
      ownPanel: false,
    },
    locked: over.locked ?? true,
    type: 'indicator',
    created_at: '',
    updated_at: '',
    deleted: false,
    ...over,
  };
}

// Stub persistence API — includes setIndicatorLocked.
vi.mock('../../api/persistence', () => ({
  listIndicators: vi.fn(async () => []),
  createIndicator: vi.fn(async (p) => ({ ...p, type: 'indicator', created_at: '', updated_at: '', deleted: false, locked: false })),
  updateIndicator: vi.fn(async (_id, p) => p),
  archiveIndicator: vi.fn(async () => null),
  // Return a full IndicatorOut-shaped doc (mirrors the real backend), with the
  // requested locked flag applied.
  setIndicatorLocked: vi.fn(async (id, locked) => indicatorOut({ id, locked })),
  describePersistenceError: vi.fn((err) => err?.message || 'Unknown error'),
  isLockedError: (err) => !!err && err.status === 423,
}));

import IndicatorsPage from './IndicatorsPage';
import { listIndicators, setIndicatorLocked, updateIndicator } from '../../api/persistence';
import { AUTOSAVE_KEY } from './storageKeys';

// A user-created indicator that starts UNLOCKED.
const UNLOCKED_IND = {
  id: 'user-ind-1',
  name: 'My Unlocked Indicator',
  locked: false,
  definition: {
    code: 'def compute(series):\n    return series["price"]\n',
    doc: '',
    params: {},
    seriesMap: {},
    ownPanel: false,
  },
  created_at: '',
  updated_at: '',
  deleted: false,
  type: 'indicator',
};

// A user-created indicator that starts LOCKED.
const LOCKED_IND = {
  id: 'user-ind-2',
  name: 'My Locked Indicator',
  locked: true,
  definition: {
    code: 'def compute(series):\n    return series["price"]\n',
    doc: '',
    params: {},
    seriesMap: {},
    ownPanel: false,
  },
  created_at: '',
  updated_at: '',
  deleted: false,
  type: 'indicator',
};

function expandCustomSection() {
  const header = screen.getByTestId('category-custom');
  if (header.getAttribute('data-collapsed') === 'true') {
    fireEvent.click(header);
  }
}

beforeEach(() => {
  try { localStorage.clear(); } catch { /* ignore */ }
  localStorage.setItem(AUTOSAVE_KEY, 'false');
  vi.clearAllMocks();
  setIndicatorLocked.mockResolvedValue(indicatorOut({ locked: true }));
});

afterEach(() => {
  cleanup();
  try { localStorage.clear(); } catch { /* ignore */ }
  vi.restoreAllMocks();
});

describe('<IndicatorsPage> lock feature', () => {
  describe('built-in (readonly) indicators', () => {
    it('shows no lock toggle for built-in default indicators', async () => {
      listIndicators.mockResolvedValue([]);
      await act(async () => { render(<IndicatorsPage />); });
      // Default indicators are rendered in the DEFAULT section.
      // None should have a lock toggle.
      const lockBtns = screen.queryAllByTestId('lock-toggle-btn');
      expect(lockBtns).toHaveLength(0);
    });
  });

  describe('locked row disables edit + delete', () => {
    it('delete button is disabled for a locked indicator', async () => {
      listIndicators.mockResolvedValue([LOCKED_IND]);
      await act(async () => { render(<IndicatorsPage />); });
      expandCustomSection();
      const deleteBtn = screen.getByLabelText('Delete My Locked Indicator');
      expect(deleteBtn.disabled).toBe(true);
    });

    it('rename button is disabled for a locked indicator', async () => {
      listIndicators.mockResolvedValue([LOCKED_IND]);
      await act(async () => { render(<IndicatorsPage />); });
      expandCustomSection();
      const renameBtn = screen.getByLabelText('Rename My Locked Indicator');
      expect(renameBtn.disabled).toBe(true);
    });

    it('delete and rename buttons are enabled for an unlocked indicator', async () => {
      listIndicators.mockResolvedValue([UNLOCKED_IND]);
      await act(async () => { render(<IndicatorsPage />); });
      expandCustomSection();
      expect(screen.getByLabelText('Delete My Unlocked Indicator').disabled).toBe(false);
      expect(screen.getByLabelText('Rename My Unlocked Indicator').disabled).toBe(false);
    });
  });

  describe('lock toggle wiring', () => {
    it('clicking the lock toggle on an unlocked indicator calls setIndicatorLocked(id, true)', async () => {
      listIndicators.mockResolvedValue([UNLOCKED_IND]);
      setIndicatorLocked.mockResolvedValue(indicatorOut({ id: 'user-ind-1', locked: true }));
      await act(async () => { render(<IndicatorsPage />); });
      expandCustomSection();
      const lockBtn = screen.getByTestId('lock-toggle-btn');
      await act(async () => { fireEvent.click(lockBtn); });
      expect(setIndicatorLocked).toHaveBeenCalledWith('user-ind-1', true);
    });

    it('rolls back locked state when setIndicatorLocked rejects', async () => {
      listIndicators.mockResolvedValue([UNLOCKED_IND]);
      setIndicatorLocked.mockRejectedValueOnce(new Error('network error'));
      await act(async () => { render(<IndicatorsPage />); });
      expandCustomSection();
      const lockBtn = screen.getByTestId('lock-toggle-btn');
      await act(async () => { fireEvent.click(lockBtn); });
      await act(async () => {});
      // After rollback, the toggle should return to unlocked (data-locked="false").
      const lockBtnAfter = screen.getByTestId('lock-toggle-btn');
      expect(lockBtnAfter.getAttribute('data-locked')).toBe('false');
    });
  });

  describe('read-only editor banner when locked indicator is open', () => {
    it('shows lock banner in the editor when the selected indicator is locked', async () => {
      listIndicators.mockResolvedValue([LOCKED_IND]);
      await act(async () => { render(<IndicatorsPage />); });
      // The page auto-selects the first indicator (a built-in default). We
      // must expand CUSTOM and click the locked user indicator to select it.
      expandCustomSection();
      fireEvent.click(screen.getByText('My Locked Indicator'));
      // The banner should now appear in the editor column.
      expect(screen.getByTestId('editor-lock-banner')).toBeTruthy();
    });

    it('does not show lock banner when selected indicator is unlocked', async () => {
      listIndicators.mockResolvedValue([UNLOCKED_IND]);
      await act(async () => { render(<IndicatorsPage />); });
      expect(screen.queryByTestId('editor-lock-banner')).toBeNull();
    });

    it('does not show lock banner for built-in (readonly) indicators', async () => {
      listIndicators.mockResolvedValue([]);
      await act(async () => { render(<IndicatorsPage />); });
      // Select a default indicator (first in list).
      expect(screen.queryByTestId('editor-lock-banner')).toBeNull();
    });
  });

  describe('423 on save flips the editor to read-only', () => {
    it('a save rejected with 423 flips the indicator to locked + shows the editor banner', async () => {
      listIndicators.mockResolvedValue([UNLOCKED_IND]);
      await act(async () => { render(<IndicatorsPage />); });
      expandCustomSection();
      // Select the unlocked custom indicator.
      await act(async () => { fireEvent.click(screen.getByText('My Unlocked Indicator')); });
      expect(screen.queryByTestId('editor-lock-banner')).toBeNull();

      // Make it dirty via a rename so the manual Save button is enabled,
      // then have the save reject with HTTP 423.
      await act(async () => {
        fireEvent.dblClick(screen.getByText('My Unlocked Indicator'));
      });
      const renameInput = screen.getByLabelText('Rename My Unlocked Indicator');
      await act(async () => {
        fireEvent.change(renameInput, { target: { value: 'Renamed' } });
        fireEvent.keyDown(renameInput, { key: 'Enter' });
      });

      const e = new Error('Document is locked');
      e.status = 423;
      updateIndicator.mockRejectedValueOnce(e);

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
      });
      await act(async () => {});

      expect(updateIndicator).toHaveBeenCalled();
      // The 423 flips the LOCAL locked flag → editor lock banner appears.
      expect(screen.getByTestId('editor-lock-banner')).toBeTruthy();
    });
  });
});
