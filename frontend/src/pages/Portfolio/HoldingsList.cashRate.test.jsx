// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import HoldingsList from './HoldingsList';

afterEach(() => { cleanup(); });

function renderWithCashLeg(overrides = {}) {
  const onUpdateLeg = vi.fn();
  const onAddCashLeg = vi.fn();
  const legs = [
    {
      id: 'cash-1',
      label: 'Cash (USD 1M rate)',
      type: 'cash_rate',
      weight: 100,
      cash_rate: { kind: 'flat', rate_pct: 1.0, compound: true },
      ...overrides,
    },
  ];
  render(
    <HoldingsList
      legs={legs}
      legDateRanges={{}}
      onUpdateLeg={onUpdateLeg}
      onRemoveLeg={vi.fn()}
      onOpenAddModal={() => {}}
      onOpenSignalModal={() => {}}
      onAddCashLeg={onAddCashLeg}
      onEditLeg={vi.fn()}
    />,
  );
  return { onUpdateLeg, onAddCashLeg };
}

describe('<HoldingsList> cash-rate leg (F4)', () => {
  it('renders the + Add Cash button and fires onAddCashLeg', () => {
    const { onAddCashLeg } = renderWithCashLeg();
    const btn = screen.getByTestId('add-cash-btn');
    fireEvent.click(btn);
    expect(onAddCashLeg).toHaveBeenCalledTimes(1);
  });

  it('shows the Cash type badge and a flat-rate editor', () => {
    renderWithCashLeg();
    expect(screen.getByText('Cash')).toBeTruthy();
    const input = screen.getByTestId('cash-rate-input-cash-1');
    expect(input.value).toBe('1');
    expect(screen.getByText(/%\/yr \(flat\)/)).toBeTruthy();
  });

  it('editing the rate calls onUpdateLeg with the new flat source', () => {
    const { onUpdateLeg } = renderWithCashLeg();
    const input = screen.getByTestId('cash-rate-input-cash-1');
    fireEvent.change(input, { target: { value: '4.5' } });
    expect(onUpdateLeg).toHaveBeenCalledWith(0, {
      cash_rate: { kind: 'flat', rate_pct: 4.5, compound: true },
    });
  });

  it('a series source shows its instrument reference read-only', () => {
    renderWithCashLeg({
      cash_rate: { kind: 'series', collection: 'FUT_RATE', symbol: 'RATE_USD', unit: 'percent' },
    });
    expect(screen.getByText('FUT_RATE/RATE_USD')).toBeTruthy();
    expect(screen.getByText('rate series')).toBeTruthy();
  });
});
