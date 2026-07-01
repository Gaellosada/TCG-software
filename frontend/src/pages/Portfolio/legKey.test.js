import { describe, it, expect } from 'vitest';
import { legsToRangesKey } from './legKey';

describe('legsToRangesKey', () => {
  it('returns empty string for no legs', () => {
    expect(legsToRangesKey([])).toBe('');
  });

  it('ignores label and weight changes (same key)', () => {
    const a = [{ type: 'instrument', collection: 'EQ', symbol: 'AAPL', label: 'L1', weight: 10 }];
    const b = [{ type: 'instrument', collection: 'EQ', symbol: 'AAPL', label: 'Renamed', weight: 55 }];
    expect(legsToRangesKey(a)).toBe(legsToRangesKey(b));
  });

  it('produces a different key when collection or symbol changes', () => {
    const a = [{ type: 'instrument', collection: 'EQ', symbol: 'AAPL' }];
    const b = [{ type: 'instrument', collection: 'EQ', symbol: 'MSFT' }];
    expect(legsToRangesKey(a)).not.toBe(legsToRangesKey(b));
  });

  it('encodes continuous legs with every config field', () => {
    const key = legsToRangesKey([{
      type: 'continuous',
      collection: 'FUT_ES',
      strategy: 'front_month',
      adjustment: 'none',
      cycle: 'all',
      rollOffset: 2,
    }]);
    expect(key).toBe('c:FUT_ES:front_month:none:all:2');
  });

  it('encodes signal legs including every input instrument', () => {
    const key = legsToRangesKey([{
      type: 'signal',
      signalId: 'sig-1',
      signalSpec: {
        inputs: [
          { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
          { id: 'Y', instrument: null },
        ],
      },
    }]);
    expect(key).toBe('s:sig-1:[i:INDEX:SPX,null]');
  });

  it('joins multiple legs with |', () => {
    const key = legsToRangesKey([
      { type: 'instrument', collection: 'EQ', symbol: 'AAPL' },
      { type: 'instrument', collection: 'EQ', symbol: 'MSFT' },
    ]);
    expect(key).toBe('i:EQ:AAPL|i:EQ:MSFT');
  });

  // ── option_stream leg tests ──

  it('encodes option_stream legs with all discriminating fields', () => {
    const key = legsToRangesKey([{
      type: 'option_stream',
      collection: 'OPT_SP_500',
      option_type: 'C',
      cycle: null,
      maturity: { kind: 'next_third_friday', offset_months: 0 },
      selection: { kind: 'by_delta', target: 0.25 },
      stream: 'mid',
    }]);
    expect(key).toContain('o:');
    expect(key).toContain('OPT_SP_500');
    expect(key).toContain('C');
    expect(key).toContain('mid');
  });

  it('different streams produce different keys for option_stream', () => {
    const base = {
      type: 'option_stream', collection: 'OPT_SP_500', option_type: 'C',
      cycle: null, maturity: { kind: 'next_third_friday' },
      selection: { kind: 'by_delta' },
    };
    const midKey = legsToRangesKey([{ ...base, stream: 'mid' }]);
    const ivKey = legsToRangesKey([{ ...base, stream: 'iv' }]);
    expect(midKey).not.toBe(ivKey);
  });

  it('different selections produce different keys for option_stream', () => {
    const base = {
      type: 'option_stream', collection: 'OPT_SP_500', option_type: 'C',
      cycle: null, maturity: { kind: 'next_third_friday' }, stream: 'mid',
    };
    const deltaKey = legsToRangesKey([{ ...base, selection: { kind: 'by_delta' } }]);
    const strikeKey = legsToRangesKey([{ ...base, selection: { kind: 'by_strike' } }]);
    expect(deltaKey).not.toBe(strikeKey);
  });

  it('different selection targets produce different keys for option_stream', () => {
    const base = {
      type: 'option_stream', collection: 'OPT_SP_500', option_type: 'C',
      cycle: null, maturity: { kind: 'next_third_friday' }, stream: 'mid',
    };
    const k25 = legsToRangesKey([{ ...base, selection: { kind: 'by_delta', target: 0.25 } }]);
    const k50 = legsToRangesKey([{ ...base, selection: { kind: 'by_delta', target: 0.50 } }]);
    expect(k25).not.toBe(k50);
  });

  it('different option types produce different keys', () => {
    const base = {
      type: 'option_stream', collection: 'OPT_SP_500', cycle: null,
      maturity: { kind: 'next_third_friday' }, selection: { kind: 'by_delta' },
      stream: 'mid',
    };
    const callKey = legsToRangesKey([{ ...base, option_type: 'C' }]);
    const putKey = legsToRangesKey([{ ...base, option_type: 'P' }]);
    expect(callKey).not.toBe(putKey);
  });

  it('different roll_offset produces different keys (it shifts contract selection)', () => {
    const base = {
      type: 'option_stream', collection: 'OPT_SP_500', option_type: 'C', cycle: null,
      maturity: { kind: 'next_third_friday' }, selection: { kind: 'by_delta' },
      stream: 'mid', adjustment: 'none',
    };
    const k0 = legsToRangesKey([{ ...base, roll_offset: { value: 0, unit: 'days' } }]);
    const k5 = legsToRangesKey([{ ...base, roll_offset: { value: 5, unit: 'days' } }]);
    expect(k0).not.toBe(k5);
  });

  it('the roll_offset UNIT is part of the key (5 days != 5 months)', () => {
    const base = {
      type: 'option_stream', collection: 'OPT_SP_500', option_type: 'C', cycle: null,
      maturity: { kind: 'next_third_friday' }, selection: { kind: 'by_delta' },
      stream: 'mid',
    };
    const days = legsToRangesKey([{ ...base, roll_offset: { value: 5, unit: 'days' } }]);
    const months = legsToRangesKey([{ ...base, roll_offset: { value: 5, unit: 'months' } }]);
    expect(days).not.toBe(months);
  });

  it('adjustment does NOT affect the option_stream key (option streams have no back-adjustment)', () => {
    const base = {
      type: 'option_stream', collection: 'OPT_SP_500', option_type: 'C', cycle: null,
      maturity: { kind: 'next_third_friday' }, selection: { kind: 'by_delta' },
      stream: 'mid', roll_offset: { value: 0, unit: 'days' },
    };
    // A stray `adjustment` key (e.g. a legacy leg) must not change the key —
    // it is not part of the option-stream identity.
    const plain = legsToRangesKey([{ ...base }]);
    const stray = legsToRangesKey([{ ...base, adjustment: 'ratio' }]);
    expect(plain).toBe(stray);
  });

  it('missing roll_offset defaults to {0, days} in the key (legacy legs)', () => {
    const legacy = {
      type: 'option_stream', collection: 'OPT_SP_500', option_type: 'C', cycle: null,
      maturity: { kind: 'next_third_friday' }, selection: { kind: 'by_delta' },
      stream: 'mid',
    };
    const explicit = { ...legacy, roll_offset: { value: 0, unit: 'days' } };
    // A legacy leg (no roll_offset) keys identically to one with the default
    // — so loading old state doesn't spuriously invalidate the range cache.
    expect(legsToRangesKey([legacy])).toBe(legsToRangesKey([explicit]));
  });
});
