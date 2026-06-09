// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import SignalsList from './SignalsList';
import styles from './Signals.module.css';

afterEach(cleanup);

const SAMPLE = [
  { id: 's1', name: 'Signal Alpha', category: 'RESEARCH' },
  { id: 's2', name: 'Signal Beta', category: 'DEV' },
];

function defaultProps(overrides = {}) {
  return {
    signals: SAMPLE,
    selectedId: 's1',
    onSelect: vi.fn(),
    onAdd: vi.fn(),
    onDelete: vi.fn(),
    onRename: vi.fn(),
    search: '',
    onSearchChange: vi.fn(),
    category: 'RESEARCH',
    onCategoryChange: vi.fn(),
    onChangeItemCat: vi.fn(),
    onSetSignalLocked: vi.fn(),
    loading: false,
    ...overrides,
  };
}

describe('<SignalsList>', () => {
  it('renders signal rows with the correct testids', () => {
    render(<SignalsList {...defaultProps()} />);
    expect(screen.getByTestId('signal-row-s1')).toBeTruthy();
    expect(screen.getByTestId('signal-row-s2')).toBeTruthy();
  });

  it('invokes onSelect when a row is clicked', () => {
    const props = defaultProps();
    render(<SignalsList {...props} />);
    fireEvent.click(screen.getByTestId('signal-row-s2'));
    expect(props.onSelect).toHaveBeenCalledWith('s2');
  });

  it('invokes onAdd when + New is clicked', () => {
    const props = defaultProps();
    render(<SignalsList {...props} />);
    fireEvent.click(screen.getByTestId('add-signal-btn'));
    expect(props.onAdd).toHaveBeenCalledOnce();
  });

  it('invokes onDelete when delete button is clicked', () => {
    const props = defaultProps();
    render(<SignalsList {...props} />);
    const deleteBtn = screen.getAllByRole('button', { name: /delete signal alpha/i })[0];
    fireEvent.click(deleteBtn);
    expect(props.onDelete).toHaveBeenCalledWith('s1');
  });

  it('shows empty-state hint when signals list is empty', () => {
    render(<SignalsList {...defaultProps({ signals: [] })} />);
    expect(screen.getByText(/no signals yet/i)).toBeTruthy();
  });

  // --- hover-reveal icon class structure (Bullet #5) ---
  // jsdom does not compute :hover styles, so we assert structural requirements:
  // the iconBtn and deleteBtn buttons carry the CSS module className that the
  // CSS hover/focus-within rules target.

  it('rename (iconBtn) button carries the iconBtn CSS class', () => {
    render(<SignalsList {...defaultProps()} />);
    const renameBtn = screen.getByRole('button', { name: /rename signal alpha/i });
    expect(renameBtn.className).toContain(styles.iconBtn);
  });

  it('delete (deleteBtn) button carries the deleteBtn CSS class', () => {
    render(<SignalsList {...defaultProps()} />);
    const deleteBtn = screen.getByRole('button', { name: /delete signal alpha/i });
    expect(deleteBtn.className).toContain(styles.deleteBtn);
  });

  it('iconBtn and deleteBtn are children of a .row element (enabling :focus-within)', () => {
    render(<SignalsList {...defaultProps()} />);
    const renameBtn = screen.getByRole('button', { name: /rename signal alpha/i });
    const deleteBtn = screen.getByRole('button', { name: /delete signal alpha/i });
    const row = screen.getByTestId('signal-row-s1');
    expect(row.className).toContain(styles.row);
    expect(row.contains(renameBtn)).toBe(true);
    expect(row.contains(deleteBtn)).toBe(true);
  });

  it('double-clicking a row enters rename mode (input replaces iconBtn)', () => {
    render(<SignalsList {...defaultProps()} />);
    const row = screen.getByTestId('signal-row-s1');
    fireEvent.doubleClick(row);
    // rename input should now be present; iconBtn and deleteBtn hidden.
    expect(screen.getByRole('textbox', { name: /rename signal alpha/i })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /rename signal alpha/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /delete signal alpha/i })).toBeNull();
  });

  // --- Category selector (persistence layer) ---

  it('renders the category filter dropdown with all four options', () => {
    render(<SignalsList {...defaultProps()} />);
    const select = screen.getByTestId('signals-category-filter');
    const options = Array.from(select.querySelectorAll('option')).map((o) => o.value);
    expect(options).toEqual(['RESEARCH', 'DEV', 'PROD', 'ARCHIVE']);
  });

  it('shows the current category as selected in the filter', () => {
    render(<SignalsList {...defaultProps({ category: 'DEV' })} />);
    expect(screen.getByTestId('signals-category-filter').value).toBe('DEV');
  });

  it('calls onCategoryChange when the category filter is changed', () => {
    const props = defaultProps();
    render(<SignalsList {...props} />);
    fireEvent.change(screen.getByTestId('signals-category-filter'), { target: { value: 'PROD' } });
    expect(props.onCategoryChange).toHaveBeenCalledWith('PROD');
  });

  it('does NOT render category filter when onCategoryChange is not provided', () => {
    const props = defaultProps({ onCategoryChange: undefined });
    render(<SignalsList {...props} />);
    expect(screen.queryByTestId('signals-category-filter')).toBeNull();
  });

  it('renders per-row category chip select for each signal', () => {
    render(<SignalsList {...defaultProps()} />);
    expect(screen.getByTestId('signal-cat-select-s1')).toBeTruthy();
    expect(screen.getByTestId('signal-cat-select-s2')).toBeTruthy();
  });

  it('per-row category select shows the signal category', () => {
    render(<SignalsList {...defaultProps()} />);
    expect(screen.getByTestId('signal-cat-select-s1').value).toBe('RESEARCH');
    expect(screen.getByTestId('signal-cat-select-s2').value).toBe('DEV');
  });

  it('calls onChangeItemCat with correct id and value when chip changes', () => {
    const props = defaultProps();
    render(<SignalsList {...props} />);
    fireEvent.change(screen.getByTestId('signal-cat-select-s1'), { target: { value: 'PROD' } });
    expect(props.onChangeItemCat).toHaveBeenCalledWith('s1', 'PROD');
  });

  it('shows loading hint when loading=true', () => {
    render(<SignalsList {...defaultProps({ loading: true, signals: [] })} />);
    expect(screen.getByText('Loading...')).toBeTruthy();
  });

  // --- Lock toggle (Feature 2) ---

  it('renders a LockToggle per row when onSetSignalLocked is provided', () => {
    render(<SignalsList {...defaultProps()} />);
    expect(screen.getAllByTestId('lock-toggle-btn')).toHaveLength(2);
  });

  it('does NOT render LockToggle when onSetSignalLocked is omitted', () => {
    render(<SignalsList {...defaultProps({ onSetSignalLocked: undefined })} />);
    expect(screen.queryByTestId('lock-toggle-btn')).toBeNull();
  });

  it('clicking the padlock on an UNLOCKED signal calls onSetSignalLocked(id, true) immediately', () => {
    const props = defaultProps();
    render(<SignalsList {...props} />);
    // Row s1 is unlocked → first lock toggle.
    const [toggle] = screen.getAllByTestId('lock-toggle-btn');
    fireEvent.click(toggle);
    expect(props.onSetSignalLocked).toHaveBeenCalledWith('s1', true);
  });

  it('disables rename, category and delete on a LOCKED row but keeps the lock toggle active', () => {
    const signals = [{ id: 's1', name: 'Locked One', category: 'RESEARCH', locked: true }];
    render(<SignalsList {...defaultProps({ signals, selectedId: 's1' })} />);
    expect(screen.getByRole('button', { name: /rename locked one/i }).disabled).toBe(true);
    expect(screen.getByRole('button', { name: /delete locked one/i }).disabled).toBe(true);
    expect(screen.getByTestId('signal-cat-select-s1').disabled).toBe(true);
    // Lock toggle stays clickable so the user can unlock.
    expect(screen.getByTestId('lock-toggle-btn').disabled).toBe(false);
  });

  it('a locked row exposes data-locked="true"; an unlocked row "false"', () => {
    const signals = [
      { id: 's1', name: 'Locked', category: 'RESEARCH', locked: true },
      { id: 's2', name: 'Open', category: 'RESEARCH', locked: false },
    ];
    render(<SignalsList {...defaultProps({ signals })} />);
    expect(screen.getByTestId('signal-row-s1').getAttribute('data-locked')).toBe('true');
    expect(screen.getByTestId('signal-row-s2').getAttribute('data-locked')).toBe('false');
  });

  it('clicking the padlock on a LOCKED signal opens the unlock confirm dialog (does not unlock immediately)', () => {
    const signals = [{ id: 's1', name: 'Locked One', category: 'RESEARCH', locked: true }];
    const props = defaultProps({ signals, selectedId: 's1' });
    render(<SignalsList {...props} />);
    fireEvent.click(screen.getByTestId('lock-toggle-btn'));
    // Neutral ConfirmDialog appears; onSetSignalLocked NOT yet called.
    expect(screen.getByTestId('confirm-dialog')).toBeTruthy();
    expect(props.onSetSignalLocked).not.toHaveBeenCalled();
    // Confirming the unlock calls onSetSignalLocked(id, false).
    fireEvent.click(screen.getByRole('button', { name: /^unlock$/i }));
    expect(props.onSetSignalLocked).toHaveBeenCalledWith('s1', false);
  });

  it('does NOT enter rename mode on double-click when the row is locked', () => {
    const signals = [{ id: 's1', name: 'Locked One', category: 'RESEARCH', locked: true }];
    render(<SignalsList {...defaultProps({ signals, selectedId: 's1' })} />);
    fireEvent.doubleClick(screen.getByTestId('signal-row-s1'));
    expect(screen.queryByRole('textbox', { name: /rename locked one/i })).toBeNull();
  });
});
