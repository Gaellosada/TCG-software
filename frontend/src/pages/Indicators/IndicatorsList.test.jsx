// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/react';
import IndicatorsList from './IndicatorsList';

afterEach(() => {
  cleanup();
  try { localStorage.clear(); } catch { /* ignore */ }
});

beforeEach(() => {
  try { localStorage.clear(); } catch { /* ignore */ }
});

const SAMPLE = [
  { id: 'd1', name: 'SMA', readonly: true },
  { id: 'u1', name: 'My RSI', readonly: false },
  { id: 'u2', name: 'My MACD' },
];

function defaultProps(overrides = {}) {
  return {
    indicators: SAMPLE,
    selectedId: 'd1',
    onSelect: vi.fn(),
    onAdd: vi.fn(),
    onDelete: vi.fn(),
    onRename: vi.fn(),
    onSetIndicatorLocked: vi.fn(),
    search: '',
    onSearchChange: vi.fn(),
    ...overrides,
  };
}

describe('<IndicatorsList>', () => {
  it('groups indicators under DEFAULT and CUSTOM headers when search is empty', () => {
    render(<IndicatorsList {...defaultProps()} />);
    expect(screen.getByTestId('category-default')).toBeTruthy();
    expect(screen.getByTestId('category-custom')).toBeTruthy();
    // + New button lives inside the CUSTOM header.
    const addBtn = screen.getByRole('button', { name: /new indicator/i });
    const customHeader = screen.getByTestId('category-custom');
    expect(customHeader.contains(addBtn)).toBe(true);
  });

  it('hides both category headers when the search query is non-empty', () => {
    render(<IndicatorsList {...defaultProps({ search: 'rsi' })} />);
    expect(screen.queryByTestId('category-default')).toBeNull();
    expect(screen.queryByTestId('category-custom')).toBeNull();
    // + New is not rendered while searching.
    expect(screen.queryByRole('button', { name: /new indicator/i })).toBeNull();
  });

  it('shows an empty-state hint under CUSTOM when no user indicators exist', () => {
    render(
      <IndicatorsList
        {...defaultProps({
          indicators: [{ id: 'd1', name: 'SMA', readonly: true }],
        })}
      />,
    );
    // CUSTOM starts expanded — empty state hint is visible immediately.
    expect(screen.getByText(/no custom indicators yet/i)).toBeTruthy();
  });

  it('omits the DEFAULT section header when no read-only indicators exist', () => {
    render(
      <IndicatorsList
        {...defaultProps({
          indicators: [{ id: 'u1', name: 'My RSI', readonly: false }],
        })}
      />,
    );
    expect(screen.queryByTestId('category-default')).toBeNull();
    expect(screen.getByTestId('category-custom')).toBeTruthy();
  });

  it('invokes onSelect when a row is clicked', () => {
    const props = defaultProps();
    render(<IndicatorsList {...props} />);
    // Both sections start expanded — rows are visible immediately.
    fireEvent.click(screen.getByText('My RSI'));
    expect(props.onSelect).toHaveBeenCalledWith('u1');
  });

  it('invokes onAdd when + New is clicked', () => {
    const props = defaultProps();
    render(<IndicatorsList {...props} />);
    fireEvent.click(screen.getByRole('button', { name: /new indicator/i }));
    expect(props.onAdd).toHaveBeenCalledOnce();
  });

  it('shows "No matches." when searching with an empty filtered list', () => {
    render(
      <IndicatorsList
        {...defaultProps({ indicators: [], search: 'zzz' })}
      />,
    );
    expect(screen.getByText(/no matches/i)).toBeTruthy();
  });

  // --- collapsible sections ---------------------------------

  it('both DEFAULT and CUSTOM sections are expanded on first load with no stored preference', () => {
    expect(localStorage.getItem('tcg.indicators.listCollapsed')).toBeNull();
    render(<IndicatorsList {...defaultProps()} />);
    const defHeader = screen.getByTestId('category-default');
    const custHeader = screen.getByTestId('category-custom');
    expect(defHeader.getAttribute('data-collapsed')).toBe('false');
    expect(custHeader.getAttribute('data-collapsed')).toBe('false');
    // Both sections' items are visible.
    expect(screen.getByText('SMA')).toBeTruthy();
    expect(screen.getByText('My RSI')).toBeTruthy();
  });

  it('renders DEFAULT section expanded by default (no stored preference)', () => {
    render(<IndicatorsList {...defaultProps()} />);
    // Items are visible because DEFAULT starts expanded.
    expect(screen.getByText('SMA')).toBeTruthy();
    // Header is present with expanded attribute.
    const header = screen.getByTestId('category-default');
    expect(header.getAttribute('aria-expanded')).toBe('true');
    expect(header.getAttribute('data-collapsed')).toBe('false');
  });

  it('clicking the DEFAULT header collapses its items', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const header = screen.getByTestId('category-default');
    fireEvent.click(header);
    // Items now hidden.
    expect(screen.queryByText('SMA')).toBeNull();
    // Header reports collapsed.
    expect(header.getAttribute('data-collapsed')).toBe('true');
    // Count suffix visible when collapsed.
    expect(within(header).getByText(/\(1\)/)).toBeTruthy();
  });

  it('clicking the collapsed DEFAULT header re-expands it', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const header = screen.getByTestId('category-default');
    fireEvent.click(header); // collapse
    fireEvent.click(header); // re-expand
    expect(screen.getByText('SMA')).toBeTruthy();
    expect(header.getAttribute('data-collapsed')).toBe('false');
  });

  it('persists collapsed state to localStorage under tcg.indicators.listCollapsed', () => {
    render(<IndicatorsList {...defaultProps()} />);
    // Collapse DEFAULT (it starts expanded).
    fireEvent.click(screen.getByTestId('category-default'));
    const raw = localStorage.getItem('tcg.indicators.listCollapsed');
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw);
    // After collapsing DEFAULT, its value is true; CUSTOM stays false (expanded).
    expect(parsed.default).toBe(true);
    expect(parsed.custom).toBe(false);
  });

  it('hydrates collapsed state from localStorage on mount', () => {
    localStorage.setItem(
      'tcg.indicators.listCollapsed',
      JSON.stringify({ default: true, custom: false }),
    );
    render(<IndicatorsList {...defaultProps()} />);
    // Default section is collapsed: its items should not render.
    expect(screen.queryByText('SMA')).toBeNull();
    // Header is present with collapsed attribute.
    expect(screen.getByTestId('category-default').getAttribute('data-collapsed')).toBe('true');
    // Custom items render (not collapsed).
    expect(screen.getByText('My RSI')).toBeTruthy();
  });

  it('keeps + New visible when CUSTOM is collapsed', () => {
    render(<IndicatorsList {...defaultProps()} />);
    // Collapse CUSTOM.
    fireEvent.click(screen.getByTestId('category-custom'));
    // Items are hidden.
    expect(screen.queryByText('My RSI')).toBeNull();
    // + New still present inside the (still-rendered) header.
    expect(screen.getByRole('button', { name: /new indicator/i })).toBeTruthy();
  });

  it('Enter and Space on the header toggle the section', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const header = screen.getByTestId('category-default');
    // DEFAULT starts expanded; Enter → collapsed.
    fireEvent.keyDown(header, { key: 'Enter' });
    expect(header.getAttribute('data-collapsed')).toBe('true');
    // Space → expanded again.
    fireEvent.keyDown(header, { key: ' ' });
    expect(header.getAttribute('data-collapsed')).toBe('false');
  });

  it('while search is active, collapsed state is ignored (flat list wins)', () => {
    localStorage.setItem(
      'tcg.indicators.listCollapsed',
      JSON.stringify({ default: true, custom: true }),
    );
    render(<IndicatorsList {...defaultProps({ search: 'sma' })} />);
    // Even though persisted state says collapsed, search mode is flat
    // and both matching items should be visible (filtered by parent).
    // Simulate the parent passing only matches:
    cleanup();
    render(
      <IndicatorsList
        {...defaultProps({
          search: 'sma',
          indicators: [{ id: 'd1', name: 'SMA', readonly: true }],
        })}
      />,
    );
    expect(screen.getByText('SMA')).toBeTruthy();
    // No section headers in search mode.
    expect(screen.queryByTestId('category-default')).toBeNull();
  });

  it('clicking + New does NOT toggle the CUSTOM section', () => {
    const props = defaultProps();
    render(<IndicatorsList {...props} />);
    const customHeader = screen.getByTestId('category-custom');
    // CUSTOM starts expanded.
    expect(customHeader.getAttribute('data-collapsed')).toBe('false');
    fireEvent.click(screen.getByRole('button', { name: /new indicator/i }));
    expect(props.onAdd).toHaveBeenCalledOnce();
    // Still expanded — + New click did not toggle the section.
    expect(customHeader.getAttribute('data-collapsed')).toBe('false');
  });

  // --- Wave 3: asset-type compatibility grey-out + tooltip ---------

  describe('asset-type compatibility', () => {
    const COMPAT_INDS = [
      { id: 'sma', name: 'SMA', readonly: true, compatibleAssetTypes: ['index', 'equity'] },
      { id: 'atm', name: 'ATM IV', readonly: true, compatibleAssetTypes: ['option'] },
      // user custom — no compat declared, must always be enabled.
      // Avoid the literal name "Custom" since the category header also
      // says "Custom" and getByText would match more than one node.
      { id: 'u1', name: 'My-RSI', readonly: false },
    ];

    it('greys out indicators incompatible with currentAssetType=option', () => {
      render(<IndicatorsList
        {...defaultProps({ indicators: COMPAT_INDS, currentAssetType: 'option' })}
      />);
      // Sections start expanded — rows visible immediately.
      const smaRow = screen.getByText('SMA').closest('[role="button"]');
      const atmRow = screen.getByText('ATM IV').closest('[role="button"]');
      expect(smaRow.getAttribute('data-incompat')).toBe('true');
      expect(atmRow.getAttribute('data-incompat')).toBe('false');
    });

    it('tooltip on incompatible row mentions accepted asset types', () => {
      render(<IndicatorsList
        {...defaultProps({ indicators: COMPAT_INDS, currentAssetType: 'option' })}
      />);
      const smaRow = screen.getByText('SMA').closest('[role="button"]');
      const title = smaRow.getAttribute('title') || '';
      expect(title).toContain('option'); // current
      expect(title).toContain('index');  // accepted
      expect(title).toContain('equity'); // accepted
    });

    it('does not grey anything when currentAssetType is null', () => {
      render(<IndicatorsList
        {...defaultProps({ indicators: COMPAT_INDS, currentAssetType: null })}
      />);
      const smaRow = screen.getByText('SMA').closest('[role="button"]');
      const atmRow = screen.getByText('ATM IV').closest('[role="button"]');
      expect(smaRow.getAttribute('data-incompat')).toBe('false');
      expect(atmRow.getAttribute('data-incompat')).toBe('false');
    });

    it('user custom indicators (no compat) are never greyed', () => {
      render(<IndicatorsList
        {...defaultProps({
          indicators: COMPAT_INDS,
          currentAssetType: 'option',
          selectedId: 'atm',
        })}
      />);
      const userRow = screen.getByText('My-RSI').closest('[role="button"]');
      expect(userRow.getAttribute('data-incompat')).toBe('false');
      expect(userRow.getAttribute('title')).toBeNull();
    });

    it('greyed rows still fire onSelect (decoration only)', () => {
      const onSelect = vi.fn();
      render(<IndicatorsList
        {...defaultProps({
          indicators: COMPAT_INDS,
          currentAssetType: 'option',
          onSelect,
        })}
      />);
      fireEvent.click(screen.getByText('SMA'));
      expect(onSelect).toHaveBeenCalledWith('sma');
    });

    it('indicators with defaultSeries are never greyed (self-contained)', () => {
      const indsWithDefaults = [
        { id: 'sma', name: 'SMA', readonly: true, compatibleAssetTypes: ['index', 'equity'] },
        {
          id: 'atm',
          name: 'ATM IV',
          readonly: true,
          compatibleAssetTypes: ['option'],
          defaultSeries: {
            atm_iv: { type: 'option_stream', collection: 'OPT_SP_500' },
          },
        },
      ];
      render(<IndicatorsList
        {...defaultProps({ indicators: indsWithDefaults, currentAssetType: 'index' })}
      />);
      const smaRow = screen.getByText('SMA').closest('[role="button"]');
      const atmRow = screen.getByText('ATM IV').closest('[role="button"]');
      // SMA is compatible with index → not greyed.
      expect(smaRow.getAttribute('data-incompat')).toBe('false');
      // ATM IV is option-only but has defaultSeries → not greyed.
      expect(atmRow.getAttribute('data-incompat')).toBe('false');
    });
  });

  // --- Lock toggle ---------------------------------------------------
  describe('lock toggle', () => {
    const LOCK_INDS = [
      { id: 'd1', name: 'SMA', readonly: true },
      { id: 'u1', name: 'My RSI', readonly: false, locked: false },
      { id: 'u2', name: 'My MACD', readonly: false, locked: true },
    ];

    it('shows LockToggle only for user-created (non-readonly) indicators', () => {
      render(<IndicatorsList {...defaultProps({ indicators: LOCK_INDS })} />);
      // Both sections start expanded; lock toggles visible.
      const lockBtns = screen.queryAllByTestId('lock-toggle-btn');
      // u1 and u2 have lock buttons; d1 (readonly) does not.
      expect(lockBtns).toHaveLength(2);
    });

    it('does NOT show LockToggle for readonly (built-in) indicators', () => {
      render(<IndicatorsList
        {...defaultProps({
          indicators: [{ id: 'd1', name: 'SMA', readonly: true }],
        })}
      />);
      expect(screen.queryByTestId('lock-toggle-btn')).toBeNull();
    });

    it('disables the edit (rename) button when the indicator is locked', () => {
      render(<IndicatorsList {...defaultProps({ indicators: LOCK_INDS })} />);
      const renameBtn = screen.getByLabelText('Rename My MACD');
      expect(renameBtn.disabled).toBe(true);
    });

    it('disables the delete button when the indicator is locked', () => {
      render(<IndicatorsList {...defaultProps({ indicators: LOCK_INDS })} />);
      const deleteBtn = screen.getByLabelText('Delete My MACD');
      expect(deleteBtn.disabled).toBe(true);
    });

    it('rename and delete buttons are enabled for an unlocked user indicator', () => {
      render(<IndicatorsList {...defaultProps({ indicators: LOCK_INDS })} />);
      expect(screen.getByLabelText('Rename My RSI').disabled).toBe(false);
      expect(screen.getByLabelText('Delete My RSI').disabled).toBe(false);
    });

    it('lock toggle on unlocked indicator calls onSetIndicatorLocked(id, true)', () => {
      const onSetIndicatorLocked = vi.fn();
      render(<IndicatorsList
        {...defaultProps({ indicators: LOCK_INDS, onSetIndicatorLocked })}
      />);
      // u1 is unlocked — clicking its lock toggle should immediately call with true.
      const lockBtns = screen.queryAllByTestId('lock-toggle-btn');
      // First lock button corresponds to u1 (unlocked), second to u2 (locked).
      fireEvent.click(lockBtns[0]);
      expect(onSetIndicatorLocked).toHaveBeenCalledWith('u1', true);
    });
  });
});
