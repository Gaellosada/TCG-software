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
});
