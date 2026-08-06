// @vitest-environment jsdom
//
// The "Rate" tab surfaces the v2 RATE series (cash_rate leg source). It is
// opt-in (allowRate) — portfolio only — and ALWAYS loads its instruments from
// v2 (rates are a v2-only object), regardless of the modal's catalog source.
// Selecting a rate instrument emits a ``cash_rate`` descriptor with
// ``data_source: 'v2'`` baked in, which legConfig maps to a cash_rate leg.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import InstrumentPickerModal from './InstrumentPickerModal';

afterEach(cleanup);

vi.mock('../../api/data', () => ({
  listCollections: vi.fn(),
  listInstruments: vi.fn(),
  getAvailableCycles: vi.fn(),
}));
vi.mock('../../api/options', () => ({ getOptionRoots: vi.fn() }));
vi.mock('../../api/persistence', () => ({
  createBasket: vi.fn(),
  listBaskets: vi.fn(),
}));

import { listCollections, listInstruments, getAvailableCycles } from '../../api/data';
import { getOptionRoots } from '../../api/options';
import { createBasket, listBaskets } from '../../api/persistence';

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_SP_500']);
  vi.mocked(listInstruments).mockImplementation((collection) => {
    if (collection === 'RATE') {
      return Promise.resolve({ items: [{ symbol: 'RATE_US_CMT_1M' }] });
    }
    return Promise.resolve({ items: [{ symbol: 'SPX' }] });
  });
  vi.mocked(getAvailableCycles).mockResolvedValue(['M']);
  vi.mocked(getOptionRoots).mockResolvedValue({ roots: [] });
  vi.mocked(listBaskets).mockResolvedValue([]);
});

describe('<InstrumentPickerModal> Rate tab', () => {
  it('does NOT show the Rate tab unless allowRate is set', async () => {
    render(<InstrumentPickerModal isOpen onClose={vi.fn()} onSelect={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Indexes')).toBeTruthy());
    expect(screen.queryByTestId('picker-rate-toggle')).toBeNull();
  });

  it('shows the Rate tab, lists RATE_US_CMT_1M, and emits the cash_rate v2 descriptor', async () => {
    const onSelect = vi.fn();
    const onClose = vi.fn();
    render(
      <InstrumentPickerModal
        isOpen
        onClose={onClose}
        onSelect={onSelect}
        allowRate
        // Portfolio opens the picker with the source selector at v1 by default;
        // the Rate tab must still load its v2-only series.
        showDataSourceSelector
        defaultSource="v1"
      />,
    );
    // Rate tab present.
    await waitFor(() => expect(screen.getByTestId('picker-rate-toggle')).toBeTruthy());
    expect(screen.getByText('Rate')).toBeTruthy();

    // Expand it and pick the 1M CMT rate.
    fireEvent.click(screen.getByTestId('picker-rate-toggle'));
    await waitFor(() => expect(screen.getByTestId('rate-instrument-RATE_US_CMT_1M')).toBeTruthy());
    fireEvent.click(screen.getByTestId('rate-instrument-RATE_US_CMT_1M'));

    // The emitted descriptor is a v2 cash_rate ref.
    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect.mock.calls[0][0]).toEqual({
      type: 'cash_rate',
      collection: 'RATE',
      instrument_id: 'RATE_US_CMT_1M',
      data_source: 'v2',
    });
    expect(onClose).toHaveBeenCalledOnce();

    // The RATE list was fetched from v2 regardless of the v1 catalog source.
    expect(listInstruments).toHaveBeenCalledWith(
      'RATE',
      expect.objectContaining({ source: 'v2' }),
    );
  });
});
