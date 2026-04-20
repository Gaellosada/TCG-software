import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  loadState,
  saveState,
  SCHEMA_VERSION,
  DIRECTIONS,
  emptyRules,
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

describe('Signals storage (v2)', () => {
  it('SCHEMA_VERSION is 2', () => {
    expect(SCHEMA_VERSION).toBe(2);
  });

  it('storage key is tcg.signals.v2', () => {
    expect(SIGNALS_STORAGE_KEY).toBe('tcg.signals.v2');
  });

  it('returns empty signals when nothing persisted', () => {
    expect(loadState()).toEqual({ signals: [] });
  });

  it('returns empty signals on malformed JSON', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, 'not-json');
    expect(loadState()).toEqual({ signals: [] });
  });

  it('discards a v1 payload and emits exactly one console.warn per page load', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: 1,
      signals: [{ id: 'old', name: 'Old', rules: emptyRules() }],
    }));
    expect(loadState()).toEqual({ signals: [] });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy).toHaveBeenCalledWith('[signals] discarding incompatible v1 state');
    // Second load in the same "page load" must NOT re-emit.
    expect(loadState()).toEqual({ signals: [] });
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it('warns for any non-v2 version (e.g. future v3)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({ version: 3, signals: [] }));
    expect(loadState()).toEqual({ signals: [] });
    expect(warnSpy).toHaveBeenCalledWith('[signals] discarding incompatible v3 state');
  });

  it('does NOT write to the Indicators localStorage key', () => {
    saveState({ signals: [{ id: 's1', name: 'S1', rules: emptyRules() }] });
    expect(storage.getItem('tcg.indicators.v1')).toBe(null);
    expect(storage.getItem(SIGNALS_STORAGE_KEY)).not.toBe(null);
  });

  it('round-trips a v2 signal with instrument + weight + conditions across all directions', () => {
    const state = {
      signals: [
        {
          id: 's1',
          name: 'My signal',
          rules: {
            long_entry: [
              {
                instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
                weight: 0.4,
                conditions: [
                  { op: 'gt',
                    lhs: { kind: 'indicator', indicator_id: 'sma-20', output: 'default' },
                    rhs: { kind: 'constant', value: 0 } },
                ],
              },
            ],
            long_exit: [
              {
                instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
                weight: 0,
                conditions: [
                  { op: 'cross_below',
                    lhs: { kind: 'instrument', collection: 'INDEX', instrument_id: '^GSPC', field: 'close' },
                    rhs: { kind: 'constant', value: 100 } },
                ],
              },
            ],
            short_entry: [],
            short_exit: [
              {
                instrument: { collection: 'INDEX', instrument_id: '^NDX' },
                weight: 0,
                conditions: [
                  { op: 'rolling_gt',
                    operand: { kind: 'indicator', indicator_id: 'rsi-14', output: 'default' },
                    lookback: 3 },
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

  it('defaults missing instrument to null and missing weight to 0', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1',
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
    expect(block.instrument).toBe(null);
    expect(block.weight).toBe(0);
    expect(block.conditions).toHaveLength(1);
  });

  it('coerces bogus weight values to 0 and clips negatives to 0', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1',
          rules: {
            long_entry: [
              { instrument: null, weight: 'not-a-number', conditions: [] },
              { instrument: null, weight: -0.5, conditions: [] },
              { instrument: null, weight: 1.5, conditions: [] },
              { instrument: null, weight: Number.POSITIVE_INFINITY, conditions: [] },
            ],
            long_exit: [], short_entry: [], short_exit: [],
          },
        },
      ],
    }));
    const weights = loadState().signals[0].rules.long_entry.map((b) => b.weight);
    // bogus, negative, infinite → 0; finite positive kept verbatim (no clip
    // here — clipping is a runtime concern, storage just coerces to finite
    // non-negative).
    expect(weights).toEqual([0, 0, 1.5, 0]);
  });

  it('drops an instrument ref missing collection or instrument_id → null', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1',
          rules: {
            long_entry: [
              { instrument: { collection: 'INDEX' }, weight: 0.2, conditions: [] },
              { instrument: { instrument_id: '^GSPC' }, weight: 0.2, conditions: [] },
              { instrument: 'not-an-object', weight: 0.2, conditions: [] },
              { instrument: { collection: 'INDEX', instrument_id: '^GSPC' }, weight: 0.2, conditions: [] },
            ],
            long_exit: [], short_entry: [], short_exit: [],
          },
        },
      ],
    }));
    const instruments = loadState().signals[0].rules.long_entry.map((b) => b.instrument);
    expect(instruments).toEqual([
      null,
      null,
      null,
      { collection: 'INDEX', instrument_id: '^GSPC' },
    ]);
  });

  it('drops malformed (non-object) blocks but keeps valid ones in order', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1',
          rules: {
            long_entry: [
              null,
              'garbage',
              { instrument: null, weight: 0, conditions: [] },
              42,
              { instrument: null, weight: 0.3, conditions: [] },
            ],
            long_exit: [], short_entry: [], short_exit: [],
          },
        },
      ],
    }));
    const blocks = loadState().signals[0].rules.long_entry;
    expect(blocks).toHaveLength(2);
    expect(blocks[1].weight).toBe(0.3);
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
              { instrument: null, weight: 0, conditions: [
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
