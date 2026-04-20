// @vitest-environment jsdom
//
// iter-4: HoldingsList used to gate leg-removal on window.confirm.
// It now routes through the shared ConfirmDialog. These tests pin the
// new dialog-driven flow so we don't silently regress back.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

afterEach(() => { cleanup(); });
import HoldingsList from './HoldingsList';

function renderList(onRemoveLeg = vi.fn(), onUpdateLeg = vi.fn()) {
  const legs = [
    {
      id: 'leg-1',
      label: 'SPY',
      type: 'instrument',
      collection: 'equity_etf',
      symbol: 'SPY',
      weight: 50,
    },
    {
      id: 'leg-2',
      label: 'IEF',
      type: 'instrument',
      collection: 'bond_etf',
      symbol: 'IEF',
      weight: 50,
    },
  ];
  render(
    <HoldingsList
      legs={legs}
      legDateRanges={{}}
      onUpdateLeg={onUpdateLeg}
      onRemoveLeg={onRemoveLeg}
      onOpenAddModal={() => {}}
    />,
  );
  return { legs, onRemoveLeg };
}

describe('<HoldingsList> remove-leg confirmation', () => {
  it('clicking the row remove button opens the ConfirmDialog (not window.confirm)', () => {
    const spy = vi.spyOn(window, 'confirm');
    renderList();
    const removeBtns = screen.getAllByLabelText(/^Remove /);
    fireEvent.click(removeBtns[0]);
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
    // Message should name the leg being removed.
    expect(screen.getByText(/"SPY"/)).toBeDefined();
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it('Enter in the open dialog confirms and calls onRemoveLeg with the leg index', () => {
    const onRemoveLeg = vi.fn();
    renderList(onRemoveLeg);
    const removeBtns = screen.getAllByLabelText(/^Remove /);
    // Click the SECOND row so we verify the correct index is passed.
    fireEvent.click(removeBtns[1]);
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
    fireEvent.keyDown(document, { key: 'Enter' });
    expect(onRemoveLeg).toHaveBeenCalledTimes(1);
    expect(onRemoveLeg).toHaveBeenCalledWith(1);
    // Dialog closes after confirm.
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
  });

  it('Escape cancels without calling onRemoveLeg', () => {
    const onRemoveLeg = vi.fn();
    renderList(onRemoveLeg);
    const removeBtns = screen.getAllByLabelText(/^Remove /);
    fireEvent.click(removeBtns[0]);
    expect(screen.getByTestId('confirm-dialog')).toBeDefined();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onRemoveLeg).not.toHaveBeenCalled();
    expect(screen.queryByTestId('confirm-dialog')).toBeNull();
  });
});
