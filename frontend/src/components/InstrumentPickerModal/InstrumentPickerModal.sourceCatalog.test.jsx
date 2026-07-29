// @vitest-environment jsdom
//
// Source-aware CATALOG in the picker. When the data-source selector is shown,
// the picker loads collections / instruments / option-roots / cycles for the
// CURRENTLY-SELECTED warehouse and RELOADS on a v1⇄v2 toggle — so the offered
// list always matches the chosen source (v2 has NO VIX / forex / gold). These
// pin the fix for the "VIX shown under v2, then fails at compute" bug.

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

// Full v1 catalog vs the restricted v2 set (the real warehouse shapes).
const V1_COLLECTIONS = ['INDEX', 'ETF', 'FOREX', 'FUT_SP_500', 'FUT_VIX'];
const V2_COLLECTIONS = ['INDEX', 'FUT_SP_500'];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listCollections).mockImplementation((_ac, opts) =>
    Promise.resolve(opts?.source === 'v2' ? [...V2_COLLECTIONS] : [...V1_COLLECTIONS]));
  vi.mocked(listInstruments).mockImplementation((coll, opts) => {
    if (opts?.source === 'v2') {
      return Promise.resolve({ items: coll === 'INDEX' ? [{ symbol: 'IND_SP_500' }] : [] });
    }
    if (coll === 'INDEX') return Promise.resolve({ items: [{ symbol: 'SPX' }, { symbol: 'VIX' }] });
    if (coll === 'ETF') return Promise.resolve({ items: [{ symbol: 'SPY' }] });
    if (coll === 'FOREX') return Promise.resolve({ items: [{ symbol: 'EURUSD' }] });
    return Promise.resolve({ items: [] });
  });
  vi.mocked(getAvailableCycles).mockResolvedValue([]);
  vi.mocked(getOptionRoots).mockImplementation((opts) =>
    Promise.resolve({
      roots: opts?.source === 'v2'
        ? [{ collection: 'OPT_SP_500', name: 'SP 500', has_greeks: true }]
        : [
          { collection: 'OPT_SP_500', name: 'SP 500', has_greeks: true },
          { collection: 'OPT_VIX', name: 'VIX', has_greeks: true },
        ],
    }));
});

async function flushOpen() {
  await waitFor(() => expect(screen.getByText('Indexes')).toBeTruthy());
}

describe('<InstrumentPickerModal> source-aware catalog', () => {
  it('v1 (default): loads the full catalog — VIX and FUT_VIX are offered', async () => {
    render(
      <InstrumentPickerModal isOpen onClose={vi.fn()} onSelect={vi.fn()} showDataSourceSelector defaultSource="v1" />,
    );
    await flushOpen();

    // The catalog was fetched for v1 (no v2 source passed).
    expect(listCollections).toHaveBeenCalledWith(null, { source: 'v1' });
    expect(getOptionRoots).toHaveBeenCalledWith({ source: 'v1' });

    // Indexes → SPX + VIX both present.
    fireEvent.click(screen.getByText('Indexes'));
    await waitFor(() => expect(screen.getByText('VIX')).toBeTruthy());
    expect(screen.getByText('SPX')).toBeTruthy();

    // Futures → FUT_VIX present.
    fireEvent.click(screen.getByText('Futures'));
    await waitFor(() => expect(screen.getByText('FUT_VIX')).toBeTruthy());
    expect(screen.getByText('FUT_SP_500')).toBeTruthy();
  });

  it('v2: loads ONLY the v2 catalog — no VIX index, no FUT_VIX, no forex', async () => {
    render(
      <InstrumentPickerModal isOpen onClose={vi.fn()} onSelect={vi.fn()} showDataSourceSelector defaultSource="v2" />,
    );
    await flushOpen();

    expect(listCollections).toHaveBeenCalledWith(null, { source: 'v2' });
    expect(getOptionRoots).toHaveBeenCalledWith({ source: 'v2' });
    // v2 has no ETF/FOREX collection → those instruments were never fetched.
    expect(listInstruments).toHaveBeenCalledWith('INDEX', expect.objectContaining({ source: 'v2' }));
    expect(listInstruments).not.toHaveBeenCalledWith('FOREX', expect.anything());

    // Indexes → the v2 symbol only; VIX absent.
    fireEvent.click(screen.getByText('Indexes'));
    await waitFor(() => expect(screen.getByText('IND_SP_500')).toBeTruthy());
    expect(screen.queryByText('VIX')).toBeNull();

    // Futures → FUT_SP_500 only; FUT_VIX gone (the exact VIX-class bug).
    fireEvent.click(screen.getByText('Futures'));
    await waitFor(() => expect(screen.getByText('FUT_SP_500')).toBeTruthy());
    expect(screen.queryByText('FUT_VIX')).toBeNull();
  });

  it('toggling the source v1→v2 RELOADS the catalog and drops v1-only items', async () => {
    render(
      <InstrumentPickerModal isOpen onClose={vi.fn()} onSelect={vi.fn()} showDataSourceSelector defaultSource="v1" />,
    );
    await flushOpen();
    // Start on v1: FUT_VIX is offered.
    fireEvent.click(screen.getByText('Futures'));
    await waitFor(() => expect(screen.getByText('FUT_VIX')).toBeTruthy());

    // Flip the selector to v2.
    fireEvent.change(screen.getByTestId('picker-data-source-select'), { target: { value: 'v2' } });

    // The catalog reloads for v2. The Futures group stays expanded, so its list
    // re-renders in place with the v2 collections — FUT_VIX disappears.
    await waitFor(() => expect(listCollections).toHaveBeenCalledWith(null, { source: 'v2' }));
    await waitFor(() => expect(screen.queryByText('FUT_VIX')).toBeNull());
    expect(screen.getByText('FUT_SP_500')).toBeTruthy();
  });

  it('without the selector: catalog stays v1 (byte-identical) even if defaultSource=v2', async () => {
    render(
      <InstrumentPickerModal isOpen onClose={vi.fn()} onSelect={vi.fn()} defaultSource="v2" />,
    );
    await flushOpen();
    // No opt-in ⇒ catalogSource is forced to v1; the v2 source is never sent.
    expect(listCollections).toHaveBeenCalledWith(null, { source: 'v1' });
    fireEvent.click(screen.getByText('Futures'));
    await waitFor(() => expect(screen.getByText('FUT_VIX')).toBeTruthy());
  });
});
