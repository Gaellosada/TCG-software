import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { loadState, saveState, SCHEMA_VERSION, DIRECTIONS, emptyRules } from './storage';
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

beforeEach(() => {
  storage = createStorageStub();
  vi.stubGlobal('localStorage', storage);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Signals storage', () => {
  it('returns empty signals when nothing persisted', () => {
    expect(loadState()).toEqual({ signals: [] });
  });

  it('returns empty signals on malformed JSON', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, 'not-json');
    expect(loadState()).toEqual({ signals: [] });
  });

  it('returns empty signals on schema-version mismatch', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: 42,
      signals: [{ id: 'x', name: 'X', rules: emptyRules() }],
    }));
    expect(loadState()).toEqual({ signals: [] });
  });

  it('does NOT write to the Indicators localStorage key', () => {
    saveState({ signals: [{ id: 's1', name: 'S1', rules: emptyRules() }] });
    // Indicators namespace must be untouched.
    expect(storage.getItem('tcg.indicators.v1')).toBe(null);
    expect(storage.getItem(SIGNALS_STORAGE_KEY)).not.toBe(null);
  });

  it('round-trips a signal with rules across all four directions', () => {
    const state = {
      signals: [
        {
          id: 's1',
          name: 'My signal',
          rules: {
            long_entry: [
              { conditions: [
                { op: 'gt',
                  lhs: { kind: 'indicator', indicator_id: 'sma-20', output: 'default' },
                  rhs: { kind: 'constant', value: 0 } },
              ] },
            ],
            long_exit: [
              { conditions: [
                { op: 'cross_below',
                  lhs: { kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC', field: 'close' },
                  rhs: { kind: 'constant', value: 100 } },
              ] },
            ],
            short_entry: [],
            short_exit: [
              { conditions: [
                { op: 'rolling_gt',
                  operand: { kind: 'indicator', indicator_id: 'rsi-14', output: 'default' },
                  lookback: 3 },
              ] },
            ],
          },
        },
      ],
    };
    saveState(state);
    const loaded = loadState();
    expect(loaded).toEqual(state);
  });

  it('ensures all four direction keys are present after load, even if persisted payload omits some', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        { id: 's1', name: 'Partial', rules: { long_entry: [] } },
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
        { id: 'ok', name: 'ok', rules: emptyRules() },
      ],
    }));
    const out = loadState();
    expect(out.signals.map((s) => s.id)).toEqual(['ok']);
  });

  it('drops conditions without a string op', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1',
          name: 'weird',
          rules: {
            long_entry: [
              { conditions: [
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
