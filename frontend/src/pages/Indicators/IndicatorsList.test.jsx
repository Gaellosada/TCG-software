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

  it('shows an empty-state hint under CUSTOM when no user indicators exist (after expanding)', () => {
    render(
      <IndicatorsList
        {...defaultProps({
          indicators: [{ id: 'd1', name: 'SMA', readonly: true }],
        })}
      />,
    );
    // CUSTOM starts collapsed; expand it to see the empty state hint.
    fireEvent.click(screen.getByTestId('category-custom'));
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
    // CUSTOM starts collapsed; expand it to see the custom rows.
    fireEvent.click(screen.getByTestId('category-custom'));
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

  // --- iter-8: collapsible sections ---------------------------------

  // iter-9: both DEFAULT and CUSTOM start collapsed on first load (no stored pref).
  it('both DEFAULT and CUSTOM sections are collapsed on first load with no stored preference', () => {
    // Ensure no preference in localStorage.
    expect(localStorage.getItem('tcg.indicators.listCollapsed')).toBeNull();
    render(<IndicatorsList {...defaultProps()} />);
    const defHeader = screen.getByTestId('category-default');
    const custHeader = screen.getByTestId('category-custom');
    expect(defHeader.getAttribute('data-collapsed')).toBe('true');
    expect(custHeader.getAttribute('data-collapsed')).toBe('true');
    // Neither section's items are visible.
    expect(screen.queryByText('SMA')).toBeNull();
    expect(screen.queryByText('My RSI')).toBeNull();
  });

  it('renders DEFAULT section collapsed by default (no stored preference)', () => {
    render(<IndicatorsList {...defaultProps()} />);
    // Items are hidden because DEFAULT starts collapsed.
    expect(screen.queryByText('SMA')).toBeNull();
    // Header is present with collapsed attribute.
    const header = screen.getByTestId('category-default');
    expect(header.getAttribute('aria-expanded')).toBe('false');
    expect(header.getAttribute('data-collapsed')).toBe('true');
    // Count suffix visible when collapsed.
    expect(within(header).getByText(/\(1\)/)).toBeTruthy();
  });

  it('clicking the DEFAULT header expands its items', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const header = screen.getByTestId('category-default');
    fireEvent.click(header);
    // Items now visible.
    expect(screen.getByText('SMA')).toBeTruthy();
    // Header reports expanded.
    expect(header.getAttribute('data-collapsed')).toBe('false');
  });

  it('clicking the expanded DEFAULT header re-collapses it', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const header = screen.getByTestId('category-default');
    fireEvent.click(header); // expand
    fireEvent.click(header); // re-collapse
    expect(screen.queryByText('SMA')).toBeNull();
    expect(header.getAttribute('data-collapsed')).toBe('true');
  });

  it('persists collapsed state to localStorage under tcg.indicators.listCollapsed', () => {
    render(<IndicatorsList {...defaultProps()} />);
    // Expand DEFAULT (it starts collapsed).
    fireEvent.click(screen.getByTestId('category-default'));
    const raw = localStorage.getItem('tcg.indicators.listCollapsed');
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw);
    // After expanding DEFAULT, its value is false; CUSTOM stays true (collapsed).
    expect(parsed.default).toBe(false);
    expect(parsed.custom).toBe(true);
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

  it('keeps + New visible when CUSTOM is collapsed (default state)', () => {
    render(<IndicatorsList {...defaultProps()} />);
    // CUSTOM starts collapsed by default — items are not visible.
    expect(screen.queryByText('My RSI')).toBeNull();
    // + New still present inside the (still-rendered) header.
    expect(screen.getByRole('button', { name: /new indicator/i })).toBeTruthy();
  });

  it('Enter and Space on the header toggle the section', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const header = screen.getByTestId('category-default');
    // DEFAULT starts collapsed; Enter → expanded.
    fireEvent.keyDown(header, { key: 'Enter' });
    expect(header.getAttribute('data-collapsed')).toBe('false');
    // Space → collapsed again.
    fireEvent.keyDown(header, { key: ' ' });
    expect(header.getAttribute('data-collapsed')).toBe('true');
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
    // CUSTOM starts collapsed by default.
    expect(customHeader.getAttribute('data-collapsed')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: /new indicator/i }));
    expect(props.onAdd).toHaveBeenCalledOnce();
    // Still collapsed — + New click did not toggle the section.
    expect(customHeader.getAttribute('data-collapsed')).toBe('true');
  });
});
