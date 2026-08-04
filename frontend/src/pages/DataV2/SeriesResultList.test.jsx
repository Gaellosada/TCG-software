import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SeriesResultList from './SeriesResultList';

/*
 * NOTE ON ASSERTIONS: ``@testing-library/jest-dom`` is not installed in this
 * repo (see frontend/package.json + src/test/setup.js), so ``toBeInTheDocument``
 * / ``toBeDisabled`` would throw "Invalid Chai property". Everything below uses
 * plain assertions on real DOM properties.
 */

const ITEMS = [
  {
    serie_id: 1433194, contract_id: 88_001, type: 'bbba', freq: '1m', source: 'cme',
    contract_code: 'EW2H6 P6260.20260313',
    expiration: '2026-03-13', strike: 6260, option_type: 'put',
  },
  {
    serie_id: 1492643, contract_id: 88_002, type: 'bbba', freq: '1m', source: 'cme',
    contract_code: 'EW2H6 P6255.20260313',
    expiration: '2026-03-13', strike: 6255, option_type: 'put',
  },
];

/** An object-level serie (index / rate): every contract field is null. */
const OBJECT_LEVEL_ITEM = {
  serie_id: 42, contract_id: null, type: 'bar', freq: '1d', source: 'cme',
  contract_code: null, expiration: null, strike: null, option_type: null,
};

function renderList(props = {}) {
  return render(
    <SeriesResultList
      items={ITEMS}
      total={195}
      skip={0}
      limit={50}
      loading={false}
      error={null}
      selectedSerieId={null}
      onSelect={() => {}}
      onPageChange={() => {}}
      {...props}
    />,
  );
}

describe('SeriesResultList', () => {
  it('renders the range and total', () => {
    renderList();
    // Strengthened from the plan's `getByText(/195/)`: that matcher also passes
    // if the range is wrong or missing entirely.
    expect(screen.getByText('195 series (1-2)')).toBeTruthy();
    expect(screen.getByText('EW2H6 P6260.20260313')).toBeTruthy();
    expect(screen.getByText('EW2H6 P6255.20260313')).toBeTruthy();
  });

  it('shows an explicit empty state rather than an error', () => {
    renderList({ items: [], total: 0 });
    expect(screen.getByText(/no series match/i)).toBeTruthy();
    // An empty page is a normal outcome of a narrow filter, so it must NOT be
    // reported the way a failed fetch is.
    expect(screen.queryByText(/failed to load/i)).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders a fetch error as an error, distinguishably from an empty result', () => {
    renderList({ items: [], total: 0, error: new Error('boom') });
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.getByText(/failed to load series: boom/i)).toBeTruthy();
    // The two states must not both satisfy some loose query.
    expect(screen.queryByText(/no series match/i)).toBeNull();
    // Nothing to page through when the fetch failed.
    expect(screen.queryByRole('button', { name: /next/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /prev/i })).toBeNull();
  });

  it('shows loading without claiming the filter matched nothing', () => {
    renderList({ items: [], total: 0, loading: true });
    expect(screen.getByText(/loading series/i)).toBeTruthy();
    expect(screen.queryByText(/no series match/i)).toBeNull();
  });

  it('calls onSelect with the serie id', () => {
    const onSelect = vi.fn();
    renderList({ onSelect });
    fireEvent.click(screen.getByText('EW2H6 P6260.20260313'));
    expect(onSelect).toHaveBeenCalledWith(1433194);
    fireEvent.click(screen.getByText('EW2H6 P6255.20260313'));
    expect(onSelect).toHaveBeenLastCalledWith(1492643);
  });

  it('marks only the selected row as current', () => {
    renderList({ selectedSerieId: 1492643 });
    const selected = screen.getByText('EW2H6 P6255.20260313').closest('button');
    const other = screen.getByText('EW2H6 P6260.20260313').closest('button');
    expect(selected.getAttribute('aria-current')).toBe('true');
    expect(other.getAttribute('aria-current')).toBeNull();
  });

  it('labels an object-level serie without printing undefined', () => {
    renderList({ items: [OBJECT_LEVEL_ITEM], total: 1 });
    // contract_code is null for index / rate series — a label that assumes it
    // exists renders "undefined". With no objectSymbol supplied the row falls
    // back to the id (see the next test for the labelled case).
    expect(screen.getByText('serie 42')).toBeTruthy();
    expect(screen.queryAllByText(/undefined/).length).toBe(0);
    const button = screen.getByText('serie 42').closest('button');
    expect(button.getAttribute('title')).toBe('serie 42 — bar · 1d');
  });

  it('names an object-level serie after the object when given its symbol', () => {
    renderList({ items: [OBJECT_LEVEL_ITEM], total: 1, objectSymbol: 'IND_SP_500' });
    // The flat list this component replaced showed the object symbol here.
    // "serie 42" is a bare database id in the UI for exactly the index and rate
    // objects, which are the ones with no contracts.
    expect(screen.getByText('IND_SP_500')).toBeTruthy();
    expect(screen.queryByText('serie 42')).toBeNull();
    const button = screen.getByText('IND_SP_500').closest('button');
    expect(button.getAttribute('title')).toBe('IND_SP_500 — bar · 1d');
  });

  it('does not lend the object symbol to a contract row that lost its code', () => {
    // A CONTRACT row with no contract_code is a data defect. Naming it after
    // the object would hide that behind a plausible-looking label, and would
    // print the same name on every such row.
    renderList({
      items: [{ ...ITEMS[0], contract_code: null }],
      total: 1,
      objectSymbol: 'OPT_SP_500_EW2',
    });
    expect(screen.getByText('serie 1433194')).toBeTruthy();
    expect(screen.queryByText('OPT_SP_500_EW2')).toBeNull();
  });

  it('omits a missing meta field rather than printing null', () => {
    renderList({ items: [{ ...OBJECT_LEVEL_ITEM, freq: null }], total: 1 });
    const button = screen.getByText('serie 42').closest('button');
    expect(button.getAttribute('title')).toBe('serie 42 — bar');
    expect(screen.queryAllByText(/null/).length).toBe(0);
  });

  it('disables Prev on the first page and pages forward by limit', () => {
    const onPageChange = vi.fn();
    renderList({ skip: 0, onPageChange });
    expect(screen.getByRole('button', { name: /prev/i }).disabled).toBe(true);
    // Next must be live here — this is the other half of the "last page"
    // assertion below, so neither passes on a permanently disabled button.
    expect(screen.getByRole('button', { name: /next/i }).disabled).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(onPageChange).toHaveBeenCalledWith(50);
  });

  it('disables Next on the last page and pages back by limit', () => {
    const onPageChange = vi.fn();
    renderList({ skip: 150, onPageChange });
    expect(screen.getByRole('button', { name: /next/i }).disabled).toBe(true);
    // The other half of the "first page" assertion: proves Prev is not simply
    // always disabled.
    expect(screen.getByRole('button', { name: /prev/i }).disabled).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: /prev/i }));
    expect(onPageChange).toHaveBeenCalledWith(100);
    // And a disabled Next really is inert.
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(onPageChange).toHaveBeenCalledTimes(1);
  });

  it('disables Next when the last page is exactly full', () => {
    // total is an exact multiple of limit. A `skip + limit <= total` bound looks
    // right against a ragged last page (total=195) and still leaves Next live
    // here, paging the user onto an empty page.
    renderList({ skip: 50, total: 100, limit: 50 });
    expect(screen.getByRole('button', { name: /next/i }).disabled).toBe(true);
    expect(screen.getByRole('button', { name: /prev/i }).disabled).toBe(false);
  });

  it('never pages before the first row', () => {
    const onPageChange = vi.fn();
    // skip is not a multiple of limit, so skip - limit would go negative.
    renderList({ skip: 25, onPageChange });
    expect(screen.getByRole('button', { name: /prev/i }).disabled).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: /prev/i }));
    expect(onPageChange).toHaveBeenCalledWith(0);
  });
});
