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
  { id: 'd1', name: '20-day SMA', readonly: true },
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

  it('shows an empty-state hint under CUSTOM when no user indicators exist', () => {
    render(
      <IndicatorsList
        {...defaultProps({
          indicators: [{ id: 'd1', name: 'SMA', readonly: true }],
        })}
      />,
    );
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

  it('renders items under DEFAULT by default (expanded)', () => {
    render(<IndicatorsList {...defaultProps()} />);
    // The default indicator row is visible.
    expect(screen.getByText('20-day SMA')).toBeTruthy();
    // Header reports expanded.
    const header = screen.getByTestId('category-default');
    expect(header.getAttribute('aria-expanded')).toBe('true');
    expect(header.getAttribute('data-collapsed')).toBe('false');
  });

  it('clicking the DEFAULT header collapses its items and shows a count suffix', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const header = screen.getByTestId('category-default');
    fireEvent.click(header);
    // Items are gone.
    expect(screen.queryByText('20-day SMA')).toBeNull();
    // Header still visible with count suffix "(1)".
    expect(header.getAttribute('data-collapsed')).toBe('true');
    expect(within(header).getByText(/\(1\)/)).toBeTruthy();
  });

  it('clicking the collapsed DEFAULT header re-expands it', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const header = screen.getByTestId('category-default');
    fireEvent.click(header); // collapse
    fireEvent.click(header); // re-expand
    expect(screen.getByText('20-day SMA')).toBeTruthy();
    expect(header.getAttribute('data-collapsed')).toBe('false');
  });

  it('persists collapsed state to localStorage under tcg.indicators.listCollapsed', () => {
    render(<IndicatorsList {...defaultProps()} />);
    fireEvent.click(screen.getByTestId('category-default'));
    const raw = localStorage.getItem('tcg.indicators.listCollapsed');
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw);
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
    expect(screen.queryByText('20-day SMA')).toBeNull();
    // Header is present with collapsed attribute.
    expect(screen.getByTestId('category-default').getAttribute('data-collapsed')).toBe('true');
    // Custom items render (not collapsed).
    expect(screen.getByText('My RSI')).toBeTruthy();
  });

  it('keeps + New visible when CUSTOM is collapsed', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const customHeader = screen.getByTestId('category-custom');
    fireEvent.click(customHeader);
    // Items gone.
    expect(screen.queryByText('My RSI')).toBeNull();
    // + New still present inside the (still-rendered) header.
    expect(screen.getByRole('button', { name: /new indicator/i })).toBeTruthy();
  });

  it('Enter and Space on the header toggle the section', () => {
    render(<IndicatorsList {...defaultProps()} />);
    const header = screen.getByTestId('category-default');
    fireEvent.keyDown(header, { key: 'Enter' });
    expect(header.getAttribute('data-collapsed')).toBe('true');
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
          indicators: [{ id: 'd1', name: '20-day SMA', readonly: true }],
        })}
      />,
    );
    expect(screen.getByText('20-day SMA')).toBeTruthy();
    // No section headers in search mode.
    expect(screen.queryByTestId('category-default')).toBeNull();
  });

  it('clicking + New does NOT toggle the CUSTOM section', () => {
    const props = defaultProps();
    render(<IndicatorsList {...props} />);
    const customHeader = screen.getByTestId('category-custom');
    expect(customHeader.getAttribute('data-collapsed')).toBe('false');
    fireEvent.click(screen.getByRole('button', { name: /new indicator/i }));
    expect(props.onAdd).toHaveBeenCalledOnce();
    // Still expanded.
    expect(customHeader.getAttribute('data-collapsed')).toBe('false');
  });
});
