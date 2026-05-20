import { describe, it, expect } from 'vitest';
import { formatInstrument } from './formatInstrument';

describe('formatInstrument', () => {
  it('formats spot instrument', () => {
    expect(formatInstrument({ type: 'spot', collection: 'INDEX', instrument_id: 'SPX' }))
      .toBe('SPX (INDEX)');
  });

  it('formats continuous instrument', () => {
    const result = formatInstrument({ type: 'continuous', collection: 'FUT_SP_500', adjustment: 'ratio', cycle: 'HMUZ' });
    expect(result).toContain('FUT_SP_500');
    expect(result).toContain('ratio');
    expect(result).toContain('HMUZ');
  });

  it('formats option_stream with all fields', () => {
    const inst = {
      type: 'option_stream', collection: 'OPT_SP_500', option_type: 'C',
      cycle: 'M', stream: 'mid', selection: { kind: 'by_delta' },
    };
    const result = formatInstrument(inst);
    expect(result).toContain('OPT_SP_500');
    expect(result).toContain('C');
    expect(result).toContain('mid');
    expect(result).toContain('M');
  });

  it('formats option_stream without cycle', () => {
    const inst = {
      type: 'option_stream', collection: 'OPT_SP_500', option_type: 'P',
      cycle: null, stream: 'iv', selection: { kind: 'by_strike' },
    };
    const result = formatInstrument(inst);
    expect(result).toContain('OPT_SP_500');
    expect(result).toContain('P');
    expect(result).toContain('iv');
    expect(result).not.toContain('null');
  });

  it('returns fallback for null', () => {
    expect(formatInstrument(null)).toBe('\u2014');
  });

  it('formats inline basket with spot legs as "Basket: leg1, leg2"', () => {
    const inst = {
      type: 'basket', kind: 'inline', asset_class: 'equity',
      legs: [
        { instrument: { type: 'spot', collection: 'ETF', instrument_id: 'SPY' }, weight: 0.6 },
        { instrument: { type: 'spot', collection: 'ETF', instrument_id: 'QQQ' }, weight: 0.4 },
      ],
    };
    expect(formatInstrument(inst)).toBe('Basket: SPY, QQQ');
  });

  it('formats inline basket with continuous legs by collection', () => {
    const inst = {
      type: 'basket', kind: 'inline', asset_class: 'future',
      legs: [
        { instrument: { type: 'continuous', collection: 'FUT_ES' }, weight: 1.0 },
        { instrument: { type: 'continuous', collection: 'FUT_NQ' }, weight: -0.5 },
      ],
    };
    expect(formatInstrument(inst)).toBe('Basket: FUT_ES, FUT_NQ');
  });

  it('formats inline basket with option_stream legs showing collection\u00b7option_type', () => {
    const inst = {
      type: 'basket', kind: 'inline', asset_class: 'option',
      legs: [
        { instrument: { type: 'option_stream', collection: 'OPT_ES', option_type: 'C' }, weight: 1.0 },
        { instrument: { type: 'option_stream', collection: 'OPT_ES', option_type: 'P' }, weight: 1.0 },
      ],
    };
    expect(formatInstrument(inst)).toBe('Basket: OPT_ES\u00b7C, OPT_ES\u00b7P');
  });

  it('formats saved basket as "Basket: <basket_id>"', () => {
    const inst = { type: 'basket', kind: 'saved', basket_id: 'BSK_TECH_2026' };
    expect(formatInstrument(inst)).toBe('Basket: BSK_TECH_2026');
  });

  it('returns fallback for inline basket with no legs', () => {
    const inst = { type: 'basket', kind: 'inline', asset_class: 'equity', legs: [] };
    expect(formatInstrument(inst)).toBe('\u2014');
    expect(formatInstrument(inst, '(none)')).toBe('(none)');
  });
});
