import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  loadState,
  saveState,
  SCHEMA_VERSION,
  SECTIONS,
  MAX_ABS_WEIGHT,
  emptyRules,
  defaultSettings,
  nextInputId,
  newBlockId,
  cascadeDeleteEntry,
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

describe('Signals storage (v4)', () => {
  it('SCHEMA_VERSION is 4', () => {
    expect(SCHEMA_VERSION).toBe(4);
  });

  it('storage key is tcg.signals.v4', () => {
    expect(SIGNALS_STORAGE_KEY).toBe('tcg.signals.v4');
  });

  it('SECTIONS are exactly ["entries", "exits"]', () => {
    expect([...SECTIONS]).toEqual(['entries', 'exits']);
  });

  it('returns empty signals when nothing persisted', () => {
    expect(loadState()).toEqual({ signals: [] });
  });

  it('returns empty signals on malformed JSON', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, 'not-json');
    expect(loadState()).toEqual({ signals: [] });
  });

  it('discards any pre-v4 payload (no migration) and warns exactly once per page load', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: 3,
      signals: [{ id: 'old', name: 'Old', rules: { entries: [], exits: [] } }],
    }));
    expect(loadState()).toEqual({ signals: [] });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy).toHaveBeenCalledWith('[signals] discarding incompatible v3 state');
    expect(loadState()).toEqual({ signals: [] });
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it('discards a v2 payload the same way (no special migration code)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: 2,
      signals: [{ id: 'old', name: 'Old' }],
    }));
    expect(loadState()).toEqual({ signals: [] });
    expect(warnSpy).toHaveBeenCalledWith('[signals] discarding incompatible v2 state');
  });

  it('does NOT write to the Indicators localStorage key', () => {
    saveState({ signals: [{ id: 's1', name: 'S1', inputs: [], rules: emptyRules() }] });
    expect(storage.getItem('tcg.indicators.v1')).toBe(null);
    expect(storage.getItem(SIGNALS_STORAGE_KEY)).not.toBe(null);
  });

  it('round-trips a v4 signal with entries + exits referencing them', () => {
    const entryId = 'entry-uuid-1';
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
            entries: [
              {
                id: entryId,
                input_id: 'X',
                weight: 40,
                name: '',
                conditions: [
                  {
                    op: 'gt',
                    lhs: { kind: 'indicator', indicator_id: 'sma-20', input_id: 'X', output: 'default' },
                    rhs: { kind: 'constant', value: 0 },
                  },
                ],
              },
            ],
            exits: [
              {
                id: 'exit-uuid-1',
                name: '',
                target_entry_block_id: entryId,
                conditions: [
                  {
                    op: 'cross_below',
                    lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
                    rhs: { kind: 'constant', value: 100 },
                  },
                ],
              },
            ],
          },
          settings: { dont_repeat: true },
        },
      ],
    };
    saveState(state);
    const loaded = loadState();
    expect(loaded).toEqual(state);
  });

  it('preserves a stored dont_repeat=false even when the default is true', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 's1', inputs: [], rules: emptyRules(),
        settings: { dont_repeat: false },
      }],
    }));
    const out = loadState();
    expect(out.signals[0].settings).toEqual({ dont_repeat: false });
  });

  it('applies dont_repeat=true default when settings are missing', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{ id: 's1', name: 's1', inputs: [], rules: emptyRules() }],
    }));
    const out = loadState();
    expect(out.signals[0].settings).toEqual({ dont_repeat: true });
  });

  it('ensures both section keys are present after load even if persisted payload omits some', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        { id: 's1', name: 'Partial', inputs: [], rules: { entries: [] } },
      ],
    }));
    const out = loadState();
    expect(out.signals).toHaveLength(1);
    for (const s of SECTIONS) {
      expect(out.signals[0].rules[s]).toEqual([]);
    }
  });

  it('strips legacy block-level input_id + weight from exit blocks on load', () => {
    // Pre-v4.1 payloads may persist input_id/weight on exits; sanitiser
    // drops them so the wire payload can't violate the backend invariant.
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [],
            exits: [
              {
                id: 'exit-1',
                input_id: 'X',        // legacy — must be stripped
                weight: 42,           // legacy — must be stripped
                target_entry_block_id: 'entry-1',
                conditions: [],
              },
            ],
          },
        },
      ],
    }));
    const out = loadState();
    const ex = out.signals[0].rules.exits[0];
    expect(ex.target_entry_block_id).toBe('entry-1');
    expect('input_id' in ex).toBe(false);
    expect('weight' in ex).toBe(false);
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

  it('generates a fresh id for any block that has none on load', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [{ input_id: 'X', weight: 50, conditions: [] }],
            exits: [],
          },
        },
      ],
    }));
    const out = loadState();
    const b = out.signals[0].rules.entries[0];
    expect(typeof b.id).toBe('string');
    expect(b.id.length).toBeGreaterThan(0);
  });

  it('defaults missing input_id to "" and missing weight to 0', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [{ id: 'b1', conditions: [
              { op: 'gt',
                lhs: { kind: 'constant', value: 1 },
                rhs: { kind: 'constant', value: 0 } },
            ] }],
            exits: [],
          },
        },
      ],
    }));
    const out = loadState();
    const block = out.signals[0].rules.entries[0];
    expect(block.input_id).toBe('');
    expect(block.weight).toBe(0);
    expect(block.conditions).toHaveLength(1);
  });

  it('preserves signed weights in [-MAX_ABS_WEIGHT, +MAX_ABS_WEIGHT]', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [
              { id: 'a', input_id: '', weight: 50, conditions: [] },
              { id: 'b', input_id: '', weight: -75, conditions: [] },
              { id: 'c', input_id: '', weight: 0, conditions: [] },
            ],
            exits: [],
          },
        },
      ],
    }));
    const weights = loadState().signals[0].rules.entries.map((b) => b.weight);
    expect(weights).toEqual([50, -75, 0]);
  });

  it('clamps weights outside [-100, +100] and coerces non-finite values to 0', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [
              { id: 'a', input_id: '', weight: 'not-a-number', conditions: [] },
              { id: 'b', input_id: '', weight: 150, conditions: [] },
              { id: 'c', input_id: '', weight: -200, conditions: [] },
              { id: 'd', input_id: '', weight: Number.POSITIVE_INFINITY, conditions: [] },
            ],
            exits: [],
          },
        },
      ],
    }));
    const weights = loadState().signals[0].rules.entries.map((b) => b.weight);
    expect(weights).toEqual([0, MAX_ABS_WEIGHT, -MAX_ABS_WEIGHT, 0]);
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
            entries: [
              null,
              'garbage',
              { id: 'a', input_id: 'X', weight: 0, conditions: [] },
              42,
              { id: 'b', input_id: 'Y', weight: 30, conditions: [] },
            ],
            exits: [],
          },
        },
      ],
    }));
    const blocks = loadState().signals[0].rules.entries;
    expect(blocks).toHaveLength(2);
    expect(blocks[0].input_id).toBe('X');
    expect(blocks[1].weight).toBe(30);
  });

  it('drops conditions without a string op', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 'weird', inputs: [],
          rules: {
            entries: [
              { id: 'b1', input_id: '', weight: 0, conditions: [
                { op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } },
                { op: 42 },
                null,
              ] },
            ],
            exits: [],
          },
        },
      ],
    }));
    const out = loadState();
    expect(out.signals[0].rules.entries[0].conditions).toHaveLength(1);
  });

  it('preserves target_entry_block_id on exit blocks', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [{ id: 'entry-1', input_id: 'X', weight: 50, conditions: [] }],
            exits: [{ id: 'exit-1', input_id: 'X', weight: 0, target_entry_block_id: 'entry-1', conditions: [] }],
          },
        },
      ],
    }));
    const out = loadState();
    const exit = out.signals[0].rules.exits[0];
    expect(exit.target_entry_block_id).toBe('entry-1');
  });

  it('defaults a missing target_entry_block_id on an exit block to ""', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [],
            exits: [{ id: 'exit-1', input_id: 'X', weight: 0, conditions: [] }],
          },
        },
      ],
    }));
    const out = loadState();
    expect(out.signals[0].rules.exits[0].target_entry_block_id).toBe('');
  });

  it('does NOT add target_entry_block_id to entry blocks', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [{ id: 'e1', input_id: 'X', weight: 50, conditions: [] }],
            exits: [],
          },
        },
      ],
    }));
    const entry = loadState().signals[0].rules.entries[0];
    expect('target_entry_block_id' in entry).toBe(false);
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

describe('defaultSettings', () => {
  it('returns { dont_repeat: true } — new signals opt into the filter by default', () => {
    expect(defaultSettings()).toEqual({ dont_repeat: true });
  });
  it('returns a fresh object each call', () => {
    const a = defaultSettings();
    const b = defaultSettings();
    expect(a).not.toBe(b);
  });
});

describe('newBlockId', () => {
  it('returns a non-empty string', () => {
    const id = newBlockId();
    expect(typeof id).toBe('string');
    expect(id.length).toBeGreaterThan(0);
  });
  it('returns distinct ids on consecutive calls', () => {
    const a = newBlockId();
    const b = newBlockId();
    expect(a).not.toBe(b);
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

describe('cascadeDeleteEntry', () => {
  const sig = {
    id: 's1', name: 's1',
    inputs: [],
    rules: {
      entries: [
        { id: 'e1', input_id: 'X', weight: 50, conditions: [] },
        { id: 'e2', input_id: 'X', weight: -30, conditions: [] },
      ],
      exits: [
        { id: 'x1', input_id: 'X', weight: 0, target_entry_block_id: 'e1', conditions: [] },
        { id: 'x2', input_id: 'X', weight: 0, target_entry_block_id: 'e2', conditions: [] },
        { id: 'x3', input_id: 'X', weight: 0, target_entry_block_id: 'e1', conditions: [] },
      ],
    },
    settings: { dont_repeat: true },
  };

  it('removes the entry and every exit referencing it', () => {
    const next = cascadeDeleteEntry(sig, 'e1');
    expect(next.rules.entries.map((b) => b.id)).toEqual(['e2']);
    expect(next.rules.exits.map((b) => b.id)).toEqual(['x2']);
  });
  it('leaves untouched entries and unrelated exits alone', () => {
    const next = cascadeDeleteEntry(sig, 'e2');
    expect(next.rules.entries.map((b) => b.id)).toEqual(['e1']);
    expect(next.rules.exits.map((b) => b.id)).toEqual(['x1', 'x3']);
  });
  it('is a no-op (but still a new object) when the entryId is unknown', () => {
    const next = cascadeDeleteEntry(sig, 'missing');
    expect(next).not.toBe(sig);
    expect(next.rules.entries).toHaveLength(2);
    expect(next.rules.exits).toHaveLength(3);
  });
  it('does not mutate the original signal', () => {
    const originalExitsLen = sig.rules.exits.length;
    cascadeDeleteEntry(sig, 'e1');
    expect(sig.rules.exits.length).toBe(originalExitsLen);
  });
});
