// @vitest-environment jsdom
//
// Per-instrument data-source at CREATION. When `showDataSourceSelector` is set,
// the modal renders a compact source selector (seeded from `defaultSource`) and
// stamps `data_source: 'v2'` onto the emitted ref — ONLY for v2, NEVER onto a
// basket, and NEVER when the selector is not opted into. These pin the emit-
// only-when-v2 byte-identity invariant at the picker boundary.

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

const MOCK_ROOTS = [
  { collection: 'OPT_SP_500', root_label: 'SP 500', name: 'SP 500', has_greeks: true },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listCollections).mockResolvedValue(['INDEX', 'ETF', 'FUT_SP_500']);
  vi.mocked(listInstruments).mockResolvedValue({ items: [{ symbol: 'SPX' }] });
  vi.mocked(getAvailableCycles).mockResolvedValue(['M']);
  vi.mocked(getOptionRoots).mockResolvedValue({ roots: MOCK_ROOTS });
  vi.mocked(listBaskets).mockResolvedValue([]);
  vi.mocked(createBasket).mockResolvedValue({ id: 'BSK_NEW', name: 'Test' });
});

async function flushAsync() {
  await waitFor(() => {
    expect(screen.queryAllByText(/Indexes|Assets|Futures|Options/).length).toBeGreaterThan(0);
  });
}

// Expand the Indexes group and click the SPX spot instrument.
async function pickSpot() {
  await waitFor(() => expect(screen.getByText('Indexes')).toBeTruthy());
  fireEvent.click(screen.getByText('Indexes'));
  await waitFor(() => expect(screen.getByText('SPX')).toBeTruthy());
  fireEvent.click(screen.getByText('SPX'));
}

describe('<InstrumentPickerModal> per-instrument data source', () => {
  it('does NOT render the source selector unless opted in', async () => {
    render(<InstrumentPickerModal isOpen onClose={vi.fn()} onSelect={vi.fn()} />);
    await flushAsync();
    expect(screen.queryByTestId('picker-data-source-select')).toBeNull();
  });

  it('opted-in: a spot pick with the default seed left at v1 emits NO data_source key (byte-identity)', async () => {
    const onSelect = vi.fn();
    render(
      <InstrumentPickerModal
        isOpen
        onClose={vi.fn()}
        onSelect={onSelect}
        showDataSourceSelector
        defaultSource="v1"
      />,
    );
    await flushAsync();
    expect(screen.getByTestId('picker-data-source-select').value).toBe('v1');
    await pickSpot();
    expect(onSelect).toHaveBeenCalledOnce();
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted).toEqual({ type: 'spot', collection: 'INDEX', instrument_id: 'SPX' });
    expect('data_source' in emitted).toBe(false);
  });

  it('opted-in: the selector seeds from defaultSource=v2 and a spot pick emits data_source:v2', async () => {
    const onSelect = vi.fn();
    render(
      <InstrumentPickerModal
        isOpen
        onClose={vi.fn()}
        onSelect={onSelect}
        showDataSourceSelector
        defaultSource="v2"
      />,
    );
    await flushAsync();
    expect(screen.getByTestId('picker-data-source-select').value).toBe('v2');
    await pickSpot();
    expect(onSelect.mock.calls[0][0]).toEqual({
      type: 'spot', collection: 'INDEX', instrument_id: 'SPX', data_source: 'v2',
    });
  });

  it('opted-in: interactively selecting v2 stamps data_source:v2 on the emitted ref', async () => {
    const onSelect = vi.fn();
    render(
      <InstrumentPickerModal
        isOpen
        onClose={vi.fn()}
        onSelect={onSelect}
        showDataSourceSelector
        defaultSource="v1"
      />,
    );
    await flushAsync();
    fireEvent.change(screen.getByTestId('picker-data-source-select'), { target: { value: 'v2' } });
    await pickSpot();
    expect(onSelect.mock.calls[0][0].data_source).toBe('v2');
  });

  it('opted-in: v2 flows onto an option_stream emit too', async () => {
    const onSelect = vi.fn();
    render(
      <InstrumentPickerModal
        isOpen
        onClose={vi.fn()}
        onSelect={onSelect}
        showDataSourceSelector
        defaultSource="v2"
      />,
    );
    await flushAsync();
    await waitFor(() => expect(screen.getByText('Options')).toBeTruthy());
    fireEvent.click(screen.getByTestId('picker-options-toggle'));
    await waitFor(() => expect(screen.getByTestId('option-stream-form')).toBeTruthy());
    const confirm = screen.getByTestId('option-stream-confirm');
    fireEvent.click(confirm);
    expect(onSelect).toHaveBeenCalledOnce();
    const emitted = onSelect.mock.calls[0][0];
    expect(emitted.type).toBe('option_stream');
    expect(emitted.data_source).toBe('v2');
  });
});
