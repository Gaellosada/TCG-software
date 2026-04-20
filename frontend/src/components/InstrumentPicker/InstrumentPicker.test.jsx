// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  render, screen, fireEvent, cleanup, waitFor,
} from '@testing-library/react';

afterEach(() => { cleanup(); });

import InstrumentPicker from './InstrumentPicker';

// Stub the /api/data network layer so useEffect doesn't reach the
// backend. listCollections returns a mixed spot + FUT_* set so we can
// assert the continuous-mode FUT_* filter and the spot-mode full list.
vi.mock('../../api/data', () => ({
  listCollections: vi.fn(async () => ['INDEX', 'FUT_ES', 'FUT_NQ']),
  listInstruments: vi.fn(async () => ({
    items: [{ symbol: 'SPX' }, { symbol: 'NDX' }],
    total: 2, skip: 0, limit: 0,
  })),
  getAvailableCycles: vi.fn(async () => ['HMUZ', 'FGHJKMNQUVXZ']),
}));

describe('<InstrumentPicker>', () => {
  it('switching type from spot to continuous resets dependent fields', () => {
    const onChange = vi.fn();
    const value = { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' };
    render(<InstrumentPicker value={value} onChange={onChange} />);

    const typeSelect = screen.getByTestId('instrument-picker-type');
    fireEvent.change(typeSelect, { target: { value: 'continuous' } });

    expect(onChange).toHaveBeenCalledTimes(1);
    const payload = onChange.mock.calls[0][0];
    expect(payload).toEqual({
      type: 'continuous',
      collection: '',        // cleared
      adjustment: 'none',
      cycle: null,
      rollOffset: 0,         // Sign 2: no pre-filled default value (0, not 2)
      strategy: 'front_month',
    });
    // Verify none of the spot fields leaked through.
    expect(payload).not.toHaveProperty('instrument_id');
  });

  it('switching type from continuous to spot resets dependent fields', () => {
    const onChange = vi.fn();
    const value = {
      type: 'continuous',
      collection: 'FUT_ES',
      adjustment: 'difference',
      cycle: 'HMUZ',
      rollOffset: 5,
      strategy: 'front_month',
    };
    render(<InstrumentPicker value={value} onChange={onChange} />);

    const typeSelect = screen.getByTestId('instrument-picker-type');
    fireEvent.change(typeSelect, { target: { value: 'spot' } });

    expect(onChange).toHaveBeenCalledTimes(1);
    const payload = onChange.mock.calls[0][0];
    expect(payload).toEqual({
      type: 'spot', collection: '', instrument_id: '',
    });
    // None of the continuous-specific fields leak through.
    for (const k of ['adjustment', 'cycle', 'rollOffset', 'strategy']) {
      expect(payload).not.toHaveProperty(k);
    }
  });

  it('in continuous mode, renders adjustment + cycle + rollOffset controls', async () => {
    const value = {
      type: 'continuous', collection: 'FUT_ES', adjustment: 'none',
      cycle: null, rollOffset: 0, strategy: 'front_month',
    };
    render(<InstrumentPicker value={value} onChange={() => {}} />);

    expect(screen.getByTestId('instrument-picker-adjustment')).toBeDefined();
    expect(screen.getByTestId('instrument-picker-cycle')).toBeDefined();
    expect(screen.getByTestId('instrument-picker-roll-offset')).toBeDefined();
    // In spot mode an `-instrument` select is rendered; in continuous it must not be.
    expect(screen.queryByTestId('instrument-picker-instrument')).toBeNull();
  });

  it('collection select is populated from listCollections (mocked)', async () => {
    render(<InstrumentPicker value={null} onChange={() => {}} />);
    // Wait for the collections to hydrate.
    await waitFor(() => {
      const sel = screen.getByTestId('instrument-picker-collection');
      // Default type is spot → ALL collections visible, not just FUT_*.
      const values = Array.from(sel.querySelectorAll('option')).map((o) => o.value);
      expect(values).toContain('INDEX');
      expect(values).toContain('FUT_ES');
      expect(values).toContain('FUT_NQ');
    });
  });

  it('in continuous mode, collection select only lists FUT_* entries', async () => {
    const value = {
      type: 'continuous', collection: '', adjustment: 'none',
      cycle: null, rollOffset: 0, strategy: 'front_month',
    };
    render(<InstrumentPicker value={value} onChange={() => {}} />);
    await waitFor(() => {
      const sel = screen.getByTestId('instrument-picker-collection');
      const values = Array.from(sel.querySelectorAll('option'))
        .map((o) => o.value)
        .filter((v) => v !== '');
      expect(values).toEqual(['FUT_ES', 'FUT_NQ']);
    });
  });

  it('rollOffset clamps to [0, 30]', () => {
    const onChange = vi.fn();
    const value = {
      type: 'continuous', collection: 'FUT_ES', adjustment: 'none',
      cycle: null, rollOffset: 0, strategy: 'front_month',
    };
    render(<InstrumentPicker value={value} onChange={onChange} />);
    const ro = screen.getByTestId('instrument-picker-roll-offset');

    // Upper-bound clamp — enter 99, component sends 30.
    fireEvent.change(ro, { target: { value: '99' } });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0].rollOffset).toBe(30);

    // Lower-bound clamp — enter -5, component sends 0.
    onChange.mockClear();
    fireEvent.change(ro, { target: { value: '-5' } });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0].rollOffset).toBe(0);

    // In-range passthrough.
    onChange.mockClear();
    fireEvent.change(ro, { target: { value: '7' } });
    expect(onChange.mock.calls[0][0].rollOffset).toBe(7);
  });

  it('onChange payload carries the discriminator field for each type', () => {
    // Spot payload.
    const onChange = vi.fn();
    render(<InstrumentPicker value={null} onChange={onChange} />);
    const typeSelect = screen.getByTestId('instrument-picker-type');

    // Default is spot; switch to continuous then back to confirm both
    // payload shapes include `type` as the discriminator.
    fireEvent.change(typeSelect, { target: { value: 'continuous' } });
    expect(onChange.mock.calls[0][0].type).toBe('continuous');

    // The component's `value` is still null on next event (controlled),
    // but switchType still dispatches a valid payload.
    fireEvent.change(typeSelect, { target: { value: 'spot' } });
    // switchType('spot') always dispatches a fresh spot payload.
    expect(onChange.mock.calls[onChange.mock.calls.length - 1][0].type)
      .toBe('spot');
  });
});
