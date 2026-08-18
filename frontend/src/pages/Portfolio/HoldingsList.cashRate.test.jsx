// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import HoldingsList from './HoldingsList';

afterEach(() => { cleanup(); });

function renderWithCashLeg(overrides = {}) {
  const onUpdateLeg = vi.fn();
  const legs = [
    {
      id: 'cash-1',
      label: 'Cash (USD 1M rate)',
      type: 'cash_rate',
      weight: 100,
      dataSource: 'v2',
      cash_rate: {
        collection: 'RATE', symbol: 'RATE_US_CMT_1M', unit: 'percent', compound: true,
      },
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
      onEditLeg={vi.fn()}
    />,
  );
  return { onUpdateLeg };
}

describe('<HoldingsList> cash-rate leg (F4, rate series — no flat input)', () => {
  it('shows the Cash type badge and the rate-series reference read-only', () => {
    renderWithCashLeg();
    expect(screen.getByText('Cash')).toBeTruthy();
    expect(screen.getByText('RATE/RATE_US_CMT_1M')).toBeTruthy();
    expect(screen.getByText('rate series')).toBeTruthy();
  });

  it('does NOT render a flat %/yr rate input (the flat-cash UI is removed)', () => {
    renderWithCashLeg();
    expect(screen.queryByTestId('cash-rate-input-cash-1')).toBeNull();
    expect(screen.queryByText(/%\/yr \(flat\)/)).toBeNull();
  });

  it('does NOT render a "+ Add Cash" button (cash legs are added via the Rate picker tab)', () => {
    renderWithCashLeg();
    expect(screen.queryByTestId('add-cash-btn')).toBeNull();
  });
});
