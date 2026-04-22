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
});
