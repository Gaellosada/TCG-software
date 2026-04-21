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
      onOpenSignalModal={() => {}}
    />,
  );
  return { legs, onRemoveLeg };
}

describe('<HoldingsList> signal leg support', () => {
  const signalLeg = {
    id: 1,
    label: 'My Signal',
    type: 'signal',
    signalId: 's1',
    signalName: 'Test Signal',
    signalSpec: { id: 's1', name: 'Test Signal', inputs: [], rules: {} },
    weight: 50,
    collection: null,
    symbol: null,
    strategy: null,
    adjustment: null,
    cycle: null,
    rollOffset: 0,
  };

  it('renders the "+ Add Signal" button', () => {
    render(
      <HoldingsList
        legs={[]}
        legDateRanges={{}}
        onUpdateLeg={vi.fn()}
        onRemoveLeg={vi.fn()}
        onOpenAddModal={vi.fn()}
        onOpenSignalModal={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: 'Add signal' })).toBeDefined();
    expect(screen.getByText('+ Add Signal')).toBeDefined();
  });

  it('calls onOpenSignalModal when "+ Add Signal" is clicked', () => {
    const onOpenSignalModal = vi.fn();
    render(
      <HoldingsList
        legs={[]}
        legDateRanges={{}}
        onUpdateLeg={vi.fn()}
        onRemoveLeg={vi.fn()}
        onOpenAddModal={vi.fn()}
        onOpenSignalModal={onOpenSignalModal}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add signal' }));
    expect(onOpenSignalModal).toHaveBeenCalledTimes(1);
  });

  it('renders signal leg with "Signal" badge and signal name in instrument column', () => {
    render(
      <HoldingsList
        legs={[signalLeg]}
        legDateRanges={{}}
        onUpdateLeg={vi.fn()}
        onRemoveLeg={vi.fn()}
        onOpenAddModal={vi.fn()}
        onOpenSignalModal={vi.fn()}
      />,
    );
    // Type badge should say "Signal".
    expect(screen.getByText('Signal')).toBeDefined();

    // The instrument column shows the signal name as an expandable button.
    expect(screen.getByText('Test Signal')).toBeDefined();

    // Input count shown next to signal name.
    expect(screen.getByText('0 inputs')).toBeDefined();
  });

  it('renders expandable signal inputs when signal has configured inputs', () => {
    const legWithInputs = {
      ...signalLeg,
      signalSpec: {
        id: 's1',
        name: 'Test Signal',
        inputs: [
          { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
          { id: 'Y', instrument: { type: 'continuous', collection: 'CME', adjustment: 'none', cycle: null, rollOffset: 0, strategy: 'front_month' } },
        ],
        rules: {},
      },
    };
    render(
      <HoldingsList
        legs={[legWithInputs]}
        legDateRanges={{}}
        onUpdateLeg={vi.fn()}
        onRemoveLeg={vi.fn()}
        onOpenAddModal={vi.fn()}
        onOpenSignalModal={vi.fn()}
      />,
    );

    // Shows "2 inputs"
    expect(screen.getByText('2 inputs')).toBeDefined();

    // Click expand button to reveal inputs
    fireEvent.click(screen.getByRole('button', { name: /Expand inputs/ }));

    // Input ids should be visible
    expect(screen.getByText('X')).toBeDefined();
    expect(screen.getByText('Y')).toBeDefined();

    // Instrument descriptions should be visible
    expect(screen.getByText(/SPX/)).toBeDefined();
    expect(screen.getByText(/CME/)).toBeDefined();
  });
});

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
