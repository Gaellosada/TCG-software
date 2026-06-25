// @vitest-environment jsdom
//
// Tests for PersistedPortfolioPanel:
//   - renders category selector with all four options
//   - category change fires onCategoryChange
//   - "+ Save as new" fires onSaveCurrent; disabled when saveDisabled
//   - empty state shown when portfolios list is empty
//   - rows rendered with name and per-row category select
//   - per-row category change fires onChangeItemCat
//   - archive button fires onArchive

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/react';
import PersistedPortfolioPanel from './PersistedPortfolioPanel';
import styles from './PersistedPortfolioPanel.module.css';

afterEach(cleanup);

const SAMPLE_PORTFOLIOS = [
  { id: 'p1', name: 'Alpha Portfolio', category: 'RESEARCH', instruments: [], rebalance: {}, locked: false },
  { id: 'p2', name: 'Beta Portfolio', category: 'DEV', instruments: [], rebalance: {}, locked: false },
];

function defaultProps(overrides = {}) {
  return {
    category: 'RESEARCH',
    onCategoryChange: vi.fn(),
    portfolios: SAMPLE_PORTFOLIOS,
    loading: false,
    onSaveCurrent: vi.fn(),
    saveDisabled: false,
    onChangeItemCat: vi.fn(),
    onArchive: vi.fn(),
    onSetPortfolioLocked: vi.fn(),
    ...overrides,
  };
}

describe('<PersistedPortfolioPanel>', () => {
  it('renders the category selector with all four category options', () => {
    render(<PersistedPortfolioPanel {...defaultProps()} />);
    const select = screen.getByTestId('portfolio-category-filter');
    const options = Array.from(select.querySelectorAll('option')).map((o) => o.value);
    expect(options).toEqual(['RESEARCH', 'DEV', 'PROD', 'ARCHIVE']);
  });

  it('shows the correct selected category', () => {
    render(<PersistedPortfolioPanel {...defaultProps({ category: 'DEV' })} />);
    const select = screen.getByTestId('portfolio-category-filter');
    expect(select.value).toBe('DEV');
  });

  it('calls onCategoryChange when category is changed', () => {
    const props = defaultProps();
    render(<PersistedPortfolioPanel {...props} />);
    const select = screen.getByTestId('portfolio-category-filter');
    fireEvent.change(select, { target: { value: 'PROD' } });
    expect(props.onCategoryChange).toHaveBeenCalledWith('PROD');
  });

  it('calls onSaveCurrent when "+ Save as new" is clicked', () => {
    const props = defaultProps();
    render(<PersistedPortfolioPanel {...props} />);
    fireEvent.click(screen.getByTestId('persist-portfolio-btn'));
    expect(props.onSaveCurrent).toHaveBeenCalledOnce();
  });

  it('disables "+ Save as new" when saveDisabled is true', () => {
    render(<PersistedPortfolioPanel {...defaultProps({ saveDisabled: true })} />);
    expect(screen.getByTestId('persist-portfolio-btn').disabled).toBe(true);
  });

  it('enables "+ Save as new" when saveDisabled is false', () => {
    render(<PersistedPortfolioPanel {...defaultProps({ saveDisabled: false })} />);
    expect(screen.getByTestId('persist-portfolio-btn').disabled).toBe(false);
  });

  it('shows loading hint when loading=true', () => {
    render(<PersistedPortfolioPanel {...defaultProps({ loading: true })} />);
    expect(screen.getByText('Loading...')).toBeTruthy();
  });

  it('shows empty-state message when portfolios list is empty', () => {
    render(<PersistedPortfolioPanel {...defaultProps({ portfolios: [] })} />);
    expect(screen.getByTestId('persisted-portfolio-empty')).toBeTruthy();
    expect(screen.getByText(/no saved portfolios in research/i)).toBeTruthy();
  });

  it('renders a row for each portfolio', () => {
    render(<PersistedPortfolioPanel {...defaultProps()} />);
    expect(screen.getByTestId('persisted-portfolio-row-p1')).toBeTruthy();
    expect(screen.getByTestId('persisted-portfolio-row-p2')).toBeTruthy();
    expect(screen.getByText('Alpha Portfolio')).toBeTruthy();
    expect(screen.getByText('Beta Portfolio')).toBeTruthy();
  });

  it('each row has a category chip select showing the item category', () => {
    render(<PersistedPortfolioPanel {...defaultProps()} />);
    const catSelect = screen.getByTestId('portfolio-cat-select-p1');
    expect(catSelect.value).toBe('RESEARCH');
    const catSelect2 = screen.getByTestId('portfolio-cat-select-p2');
    expect(catSelect2.value).toBe('DEV');
  });

  it('calls onChangeItemCat with correct id and new category on chip change', () => {
    const props = defaultProps();
    render(<PersistedPortfolioPanel {...props} />);
    const catSelect = screen.getByTestId('portfolio-cat-select-p1');
    fireEvent.change(catSelect, { target: { value: 'PROD' } });
    expect(props.onChangeItemCat).toHaveBeenCalledWith('p1', 'PROD');
  });

  it('calls onArchive with the correct id when archive button is clicked', () => {
    const props = defaultProps();
    render(<PersistedPortfolioPanel {...props} />);
    fireEvent.click(screen.getByTestId('archive-portfolio-p1'));
    expect(props.onArchive).toHaveBeenCalledWith('p1');
  });

  it('disables category select and archive button when row is locked', () => {
    const lockedPortfolios = [
      { id: 'p1', name: 'Alpha Portfolio', category: 'RESEARCH', locked: true },
    ];
    render(<PersistedPortfolioPanel {...defaultProps({ portfolios: lockedPortfolios })} />);
    expect(screen.getByTestId('portfolio-cat-select-p1').disabled).toBe(true);
    expect(screen.getByTestId('archive-portfolio-p1').disabled).toBe(true);
  });

  it('does not disable category select or archive when row is not locked', () => {
    const unlockedPortfolios = [
      { id: 'p1', name: 'Alpha Portfolio', category: 'RESEARCH', locked: false },
    ];
    render(<PersistedPortfolioPanel {...defaultProps({ portfolios: unlockedPortfolios })} />);
    expect(screen.getByTestId('portfolio-cat-select-p1').disabled).toBe(false);
    expect(screen.getByTestId('archive-portfolio-p1').disabled).toBe(false);
  });

  it('renders a LockToggle per row', () => {
    render(<PersistedPortfolioPanel {...defaultProps()} />);
    // Each row has a lock-toggle-btn (two rows → two buttons).
    const lockBtns = screen.getAllByTestId('lock-toggle-btn');
    expect(lockBtns).toHaveLength(2);
  });

  it('calls onSetPortfolioLocked with (id, true) when unlocked toggle is clicked', () => {
    const props = defaultProps({
      portfolios: [{ id: 'p1', name: 'Alpha Portfolio', category: 'RESEARCH', locked: false }],
    });
    render(<PersistedPortfolioPanel {...props} />);
    const lockBtn = screen.getByTestId('lock-toggle-btn');
    fireEvent.click(lockBtn);
    expect(props.onSetPortfolioLocked).toHaveBeenCalledWith('p1', true);
  });

  // Lock-on-left: the LockToggle is the row's first child, before the name.
  it('LockToggle is the first child of the row, before the name button', () => {
    render(<PersistedPortfolioPanel {...defaultProps()} />);
    const row = screen.getByTestId('persisted-portfolio-row-p1');
    const lockBtn = within(row).getByTestId('lock-toggle-btn');
    const nameBtn = within(row).getByTestId('load-portfolio-p1');
    const order = lockBtn.compareDocumentPosition(nameBtn);
    expect(order & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(row.firstElementChild.contains(lockBtn)).toBe(true);
  });

  // Category chip + archive × live inside a single .rowActions wrapper (which
  // owns the collapse/hover-reveal) nested within the row.
  it('category select and archive button live inside a .rowActions wrapper within the row', () => {
    render(<PersistedPortfolioPanel {...defaultProps()} />);
    const catSelect = screen.getByTestId('portfolio-cat-select-p1');
    const archiveBtn = screen.getByTestId('archive-portfolio-p1');
    const wrapper = catSelect.closest(`.${styles.rowActions}`);
    expect(wrapper).not.toBeNull();
    expect(wrapper.contains(catSelect)).toBe(true);
    expect(wrapper.contains(archiveBtn)).toBe(true);
    const row = screen.getByTestId('persisted-portfolio-row-p1');
    expect(row.contains(wrapper)).toBe(true);
  });
});
