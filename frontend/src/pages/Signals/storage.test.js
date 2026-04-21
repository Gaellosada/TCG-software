import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  loadState,
  saveState,
  SCHEMA_VERSION,
  DIRECTIONS,
  emptyRules,
  nextInputId,
  __resetIncompatibleVersionWarnedForTests,
} from './storage';
import { SIGNALS_STORAGE_KEY } from './storageKeys';

function createStorageStub() {
  const store = new Map();
  return {
    getItem: vi.fn((k) => (store.has(k) ? store.get(k) : null)),
    setItem: vi.fn((k, v) => { store.set(k, String(v)); }),
    removeItem: vi.fn((k) => { store.delete(k); }),
    clear: vi.fn(() => { store.clear(); }),
    _store: store,
  };
}

let storage;
let warnSpy;

beforeEach(() => {
  storage = createStorageStub();
  vi.stubGlobal('localStorage', storage);
  warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
  __resetIncompatibleVersionWarnedForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  warnSpy.mockRestore();
});

describe('Signals storage (v3)', () => {
  it('SCHEMA_VERSION is 3', () => {
    expect(SCHEMA_VERSION).toBe(3);
  });

  it('storage key is tcg.signals.v3', () => {
    expect(SIGNALS_STORAGE_KEY).toBe('tcg.signals.v3');
  });

  it('returns empty signals when nothing persisted', () => {
    expect(loadState()).toEqual({ signals: [] });
  });

  it('returns empty signals on malformed JSON', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, 'not-json');
    expect(loadState()).toEqual({ signals: [] });
  });

  it('discards a v2 payload and emits exactly one console.warn per page load (iter-4 hard reset)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: 2,
      signals: [{ id: 'old', name: 'Old', rules: emptyRules() }],
    }));
    expect(loadState()).toEqual({ signals: [] });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy).toHaveBeenCalledWith('[signals] discarding incompatible v2 state');
    expect(loadState()).toEqual({ signals: [] });
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it('does NOT write to the Indicators localStorage key', () => {
    saveState({ signals: [{ id: 's1', name: 'S1', inputs: [], rules: emptyRules() }] });
    expect(storage.getItem('tcg.indicators.v1')).toBe(null);
    expect(storage.getItem(SIGNALS_STORAGE_KEY)).not.toBe(null);
  });

  it('round-trips a v3 signal with inputs + blocks referencing them', () => {
    const state = {
      signals: [
        {
          id: 's1',
          name: 'My signal',
          doc: '',
          inputs: [
            { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
            {
              id: 'Y',
              instrument: {
                type: 'continuous',
                collection: 'FUT_ES',
                adjustment: 'proportional',
                cycle: 'H',
                rollOffset: 2,
                strategy: 'front_month',
              },
            },
          ],
          rules: {
            long_entry: [
              {
                input_id: 'X',
                weight: 0.4,
                conditions: [
                  {
                    op: 'gt',
                    lhs: { kind: 'indicator', indicator_id: 'sma-20', input_id: 'X', output: 'default' },
                    rhs: { kind: 'constant', value: 0 },
                  },
                ],
              },
            ],
            long_exit: [
              {
                input_id: 'X',
                weight: 0,
                conditions: [
                  {
                    op: 'cross_below',
                    lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
                    rhs: { kind: 'constant', value: 100 },
                  },
                ],
              },
            ],
            short_entry: [],
            short_exit: [
              {
                input_id: 'Y',
                weight: 0,
                conditions: [
                  {
                    op: 'rolling_gt',
                    operand: { kind: 'indicator', indicator_id: 'rsi-14', input_id: 'Y', output: 'default' },
                    lookback: 3,
                  },
                ],
              },
            ],
          },
        },
      ],
    };
    saveState(state);
    const loaded = loadState();
    expect(loaded).toEqual(state);
  });

  it('ensures all four direction keys are present after load even if persisted payload omits some', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        { id: 's1', name: 'Partial', inputs: [], rules: { long_entry: [] } },
      ],
    }));
    const out = loadState();
    expect(out.signals).toHaveLength(1);
    for (const d of DIRECTIONS) {
      expect(out.signals[0].rules[d]).toEqual([]);
    }
  });

  it('drops malformed signals without id', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        { name: 'no id' },
        { id: 'ok', name: 'ok', inputs: [], rules: emptyRules() },
      ],
    }));
    const out = loadState();
    expect(out.signals.map((s) => s.id)).toEqual(['ok']);
  });

  it('defaults missing input_id to "" and missing weight to 0', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            long_entry: [{ conditions: [
              { op: 'gt',
                lhs: { kind: 'constant', value: 1 },
                rhs: { kind: 'constant', value: 0 } },
            ] }],
            long_exit: [], short_entry: [], short_exit: [],
          },
        },
      ],
    }));
    const out = loadState();
    const block = out.signals[0].rules.long_entry[0];
    expect(block.input_id).toBe('');
    expect(block.weight).toBe(0);
    expect(block.conditions).toHaveLength(1);
  });

  it('coerces bogus weights to 0 and rejects negatives', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            long_entry: [
              { input_id: '', weight: 'not-a-number', conditions: [] },
              { input_id: '', weight: -0.5, conditions: [] },
              { input_id: '', weight: 1.5, conditions: [] },
              { input_id: '', weight: Number.POSITIVE_INFINITY, conditions: [] },
            ],
            long_exit: [], short_entry: [], short_exit: [],
          },
        },
      ],
    }));
    const weights = loadState().signals[0].rules.long_entry.map((b) => b.weight);
    expect(weights).toEqual([0, 0, 1.5, 0]);
  });

  it('keeps inputs with null instrument (user still picking)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1',
          inputs: [
            { id: 'X', instrument: { type: 'spot', collection: '', instrument_id: '' } },
            { id: 'Y', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
          ],
          rules: emptyRules(),
        },
      ],
    }));
    const out = loadState();
    const inputs = out.signals[0].inputs;
    expect(inputs).toHaveLength(2);
    expect(inputs[0].instrument).toBe(null); // Incomplete → null
    expect(inputs[1].instrument).toEqual({
      type: 'spot', collection: 'INDEX', instrument_id: 'SPX',
    });
  });

  it('drops malformed (non-object) blocks but keeps valid ones in order', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            long_entry: [
              null,
              'garbage',
              { input_id: 'X', weight: 0, conditions: [] },
              42,
              { input_id: 'Y', weight: 0.3, conditions: [] },
            ],
            long_exit: [], short_entry: [], short_exit: [],
          },
        },
      ],
    }));
    const blocks = loadState().signals[0].rules.long_entry;
    expect(blocks).toHaveLength(2);
    expect(blocks[0].input_id).toBe('X');
    expect(blocks[1].weight).toBe(0.3);
  });

  it('drops conditions without a string op', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 'weird', inputs: [],
          rules: {
            long_entry: [
              { input_id: '', weight: 0, conditions: [
                { op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } },
                { op: 42 },
                null,
              ] },
            ],
            long_exit: [], short_entry: [], short_exit: [],
          },
        },
      ],
    }));
    const out = loadState();
    expect(out.signals[0].rules.long_entry[0].conditions).toHaveLength(1);
  });

  it('tolerates setItem throwing on save', () => {
    storage.setItem.mockImplementation(() => { throw new Error('quota'); });
    expect(() => saveState({ signals: [] })).not.toThrow();
  });

  it('returns empty state when localStorage is unavailable', () => {
    vi.unstubAllGlobals();
    vi.stubGlobal('localStorage', undefined);
    expect(loadState()).toEqual({ signals: [] });
  });
});

describe('nextInputId', () => {
  it('returns X when no inputs', () => {
    expect(nextInputId([])).toBe('X');
    expect(nextInputId(null)).toBe('X');
  });
  it('returns the next free letter in alphabet order', () => {
    expect(nextInputId([{ id: 'X' }])).toBe('Y');
    expect(nextInputId([{ id: 'X' }, { id: 'Y' }])).toBe('Z');
    expect(nextInputId([{ id: 'Y' }])).toBe('X');
  });
  it('falls back to I<n> once the alphabet is exhausted', () => {
    const taken = ['X', 'Y', 'Z', 'W', 'U', 'V', 'A', 'B', 'C', 'D'].map((id) => ({ id }));
    expect(nextInputId(taken)).toBe('I1');
    expect(nextInputId([...taken, { id: 'I1' }])).toBe('I2');
  });
});
