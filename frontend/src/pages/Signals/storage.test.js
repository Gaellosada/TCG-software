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
  coerceResetCount,
  migrateV5ToV6,
  __resetIncompatibleVersionWarnedForTests,
} from './storage';
import { coerceResetCount as coerceFromBlockShape } from './blockShape';
import { __coerceResetCountForTests as coerceFromRequest } from './requestBuilder';
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

describe('Signals storage (v6)', () => {
  it('SCHEMA_VERSION is 6', () => {
    expect(SCHEMA_VERSION).toBe(6);
  });

  it('storage key is tcg.signals.v5 (key unchanged; v5→v6 migrates in place)', () => {
    expect(SIGNALS_STORAGE_KEY).toBe('tcg.signals.v5');
  });

  it('SECTIONS are exactly ["entries", "exits", "resets"]', () => {
    expect([...SECTIONS]).toEqual(['entries', 'exits', 'resets']);
  });

  it('returns empty signals when nothing persisted', () => {
    expect(loadState()).toEqual({ signals: [] });
  });

  it('returns empty signals on malformed JSON', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, 'not-json');
    expect(loadState()).toEqual({ signals: [] });
  });

  it('discards a pre-v5 (v3) payload (no migration) and warns exactly once per page load', () => {
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

  it('discards a v4 payload (clean break — no migration from v4 to v5)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: 4,
      signals: [{ id: 'old', name: 'Old', inputs: [], rules: { entries: [], exits: [] } }],
    }));
    expect(loadState()).toEqual({ signals: [] });
    expect(warnSpy).toHaveBeenCalledWith('[signals] discarding incompatible v4 state');
  });

  it('does NOT write to the Indicators localStorage key', () => {
    saveState({ signals: [{ id: 's1', name: 'S1', inputs: [], rules: emptyRules() }] });
    expect(storage.getItem('tcg.indicators.v1')).toBe(null);
    expect(storage.getItem(SIGNALS_STORAGE_KEY)).not.toBe(null);
  });

  it('round-trips a v5 signal with entries + exits referencing them', () => {
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
                adjustment: 'ratio',
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
                enabled: true,
                description: '',
                requires_reset_block_id: null,
                requires_reset_count: 1,
              },
            ],
            exits: [
              {
                id: 'exit-uuid-1',
                name: '',
                target_entry_block_names: [],
                conditions: [
                  {
                    op: 'cross_below',
                    lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
                    rhs: { kind: 'constant', value: 100 },
                  },
                ],
                enabled: true,
                description: '',
                requires_reset_block_id: null,
                requires_reset_count: 1,
              },
            ],
            resets: [],
          },
          settings: { dont_repeat: true },
        },
      ],
    };
    saveState(state);
    const loaded = loadState();
    expect(loaded).toEqual(state);
  });

  it('round-trips an option_stream instrument through save/load (legacy entry gains roll_offset default, no adjustment)', () => {
    const state = {
      signals: [
        {
          id: 's2',
          name: 'Options Signal',
          doc: '',
          inputs: [
            {
              id: 'O',
              instrument: {
                type: 'option_stream',
                collection: 'OPT_SPX',
                option_type: 'C',
                cycle: 'W3_FRI',
                maturity: { kind: 'fixed', value: '2025-06-20' },
                selection: { kind: 'delta', value: 0.3 },
                stream: 'iv',
              },
            },
          ],
          rules: emptyRules(),
          settings: { dont_repeat: true },
        },
      ],
    };
    saveState(state);
    const loaded = loadState();
    // roll_offset defaults to the unified {value:0, unit:'days'} for a legacy
    // entry that predates it — the sanitiser stamps it on every option_stream.
    // Option streams carry no back-adjustment, so no `adjustment` field.
    const inst = loaded.signals[0].inputs[0].instrument;
    expect('adjustment' in inst).toBe(false);
    expect(inst.roll_offset).toEqual({ value: 0, unit: 'days' });
    // Everything else is preserved verbatim.
    expect(inst).toMatchObject({
      type: 'option_stream',
      collection: 'OPT_SPX',
      option_type: 'C',
      cycle: 'W3_FRI',
      maturity: { kind: 'fixed', value: '2025-06-20' },
      selection: { kind: 'delta', value: 0.3 },
      stream: 'iv',
    });
  });

  it('round-trips explicit option_stream roll_offset and drops a stray adjustment', () => {
    const state = {
      signals: [
        {
          id: 's2b',
          name: 'Options Signal With Roll',
          doc: '',
          inputs: [
            {
              id: 'O',
              instrument: {
                type: 'option_stream',
                collection: 'OPT_SPX',
                option_type: 'P',
                cycle: null,
                maturity: { kind: 'nearest_to_target', target_days: 30 },
                selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
                stream: 'mid',
                // A stray adjustment must be dropped on load — option streams
                // carry no back-adjustment.  A legacy bare-int roll_offset reads
                // back as the unified {value, unit:'days'}.
                adjustment: 'ratio',
                roll_offset: 5,
              },
            },
          ],
          rules: emptyRules(),
          settings: { dont_repeat: true },
        },
      ],
    };
    saveState(state);
    const loaded = loadState();
    const inst = loaded.signals[0].inputs[0].instrument;
    expect('adjustment' in inst).toBe(false);
    expect(inst.roll_offset).toEqual({ value: 5, unit: 'days' });
    expect(inst).toMatchObject({
      type: 'option_stream',
      collection: 'OPT_SPX',
      option_type: 'P',
      stream: 'mid',
    });
  });

  // ── Issue #3: roll strategy / roll schedule survive save→load ──────────

  it('round-trips continuous strategy=end_of_month (not silently stripped)', () => {
    const state = {
      signals: [
        {
          id: 's-eom',
          name: 'EOM futures',
          inputs: [
            {
              id: 'Y',
              instrument: {
                type: 'continuous',
                collection: 'FUT_ES',
                adjustment: 'ratio',
                cycle: 'HMUZ',
                rollOffset: 2,
                strategy: 'end_of_month',
              },
            },
          ],
          rules: emptyRules(),
        },
      ],
    };
    saveState(state);
    const inst = loadState().signals[0].inputs[0].instrument;
    expect(inst.strategy).toBe('end_of_month');
  });

  it('coerces a rogue continuous strategy back to front_month', () => {
    const state = {
      signals: [
        {
          id: 's-rogue',
          name: 'Rogue strat',
          inputs: [
            {
              id: 'Y',
              instrument: {
                type: 'continuous',
                collection: 'FUT_ES',
                adjustment: 'none',
                cycle: null,
                rollOffset: 0,
                strategy: 'weekly_voodoo',
              },
            },
          ],
          rules: emptyRules(),
        },
      ],
    };
    saveState(state);
    const inst = loadState().signals[0].inputs[0].instrument;
    expect(inst.strategy).toBe('front_month');
  });

  it('round-trips the unified option_stream roll_offset {value, unit}', () => {
    const mk = (roll_offset) => ({
      signals: [
        {
          id: 's-ro',
          name: 'Opt RO',
          inputs: [
            {
              id: 'O',
              instrument: {
                type: 'option_stream',
                collection: 'OPT_SPX',
                option_type: 'C',
                cycle: null,
                maturity: { kind: 'end_of_month', offset_months: 1 },
                selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
                stream: 'mid',
                roll_offset,
              },
            },
          ],
          rules: emptyRules(),
        },
      ],
    });
    // days
    saveState(mk({ value: 4, unit: 'days' }));
    expect(loadState().signals[0].inputs[0].instrument.roll_offset).toEqual({
      value: 4,
      unit: 'days',
    });
    // months (clamped 0..12 — 13 → 12)
    saveState(mk({ value: 13, unit: 'months' }));
    expect(loadState().signals[0].inputs[0].instrument.roll_offset).toEqual({
      value: 12,
      unit: 'months',
    });
    // bogus unit → days
    saveState(mk({ value: 2, unit: 'weeks' }));
    expect(loadState().signals[0].inputs[0].instrument.roll_offset).toEqual({
      value: 2,
      unit: 'days',
    });
  });

  it('drops a legacy roll_schedule key on load (superseded by EndOfMonth maturity)', () => {
    const state = {
      signals: [
        {
          id: 's-legacy-rs',
          name: 'Legacy opt',
          inputs: [
            {
              id: 'O',
              instrument: {
                type: 'option_stream',
                collection: 'OPT_SPX',
                option_type: 'C',
                cycle: null,
                maturity: { kind: 'end_of_month', offset_months: 1 },
                selection: { kind: 'by_strike', strike: 4500 },
                stream: 'mid',
                // A short-lived #3-era field — must NOT survive the sanitiser.
                roll_schedule: 'end_of_month',
              },
            },
          ],
          rules: emptyRules(),
        },
      ],
    };
    saveState(state);
    const inst = loadState().signals[0].inputs[0].instrument;
    expect('roll_schedule' in inst).toBe(false);
    // The roll-at-month-end intent now lives in the EndOfMonth maturity.
    expect(inst.maturity).toEqual({ kind: 'end_of_month', offset_months: 1 });
  });

  it('sanitiser clamps a malformed option_stream roll_offset and drops a stray adjustment', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's2c',
          name: 'Opt Clamp',
          inputs: [
            {
              id: 'O',
              instrument: {
                type: 'option_stream',
                collection: 'OPT_SPX',
                option_type: 'C',
                cycle: null,
                maturity: { kind: 'fixed', value: '2025-06-20' },
                selection: { kind: 'by_moneyness', target: 1.0, tolerance: 0.05 },
                stream: 'iv',
                adjustment: 'bogus',     // stray key → dropped (no adjustment)
                roll_offset: 99.7,       // legacy float → trunc + clamp → {30, days}
              },
            },
          ],
          rules: emptyRules(),
        },
      ],
    }));
    const inst = loadState().signals[0].inputs[0].instrument;
    expect('adjustment' in inst).toBe(false);
    expect(inst.roll_offset).toEqual({ value: 30, unit: 'days' });
  });

  it('sanitiser rejects an option_stream with missing fields', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's3',
          name: 'Bad Opt',
          inputs: [
            { id: 'O', instrument: { type: 'option_stream', collection: 'OPT_SPX' } },
          ],
          rules: emptyRules(),
        },
      ],
    }));
    const out = loadState();
    // Incomplete option_stream → null (input kept but instrument is null)
    expect(out.signals[0].inputs[0].instrument).toBe(null);
  });

  it('sanitiser rejects an option_stream with invalid stream value', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's4',
          name: 'Bad Stream',
          inputs: [
            {
              id: 'O',
              instrument: {
                type: 'option_stream',
                collection: 'OPT_SPX',
                option_type: 'C',
                cycle: null,
                maturity: { kind: 'fixed', value: '2025-06-20' },
                selection: { kind: 'delta', value: 0.3 },
                stream: 'rogue_value',
              },
            },
          ],
          rules: emptyRules(),
        },
      ],
    }));
    const out = loadState();
    expect(out.signals[0].inputs[0].instrument).toBe(null);
  });

  it('coerces a stored dont_repeat=false to true on load', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 's1', inputs: [], rules: emptyRules(),
        settings: { dont_repeat: false },
      }],
    }));
    const out = loadState();
    expect(out.signals[0].settings).toEqual({ dont_repeat: true });
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
                target_entry_block_names: ['MyEntry'],
                conditions: [],
              },
            ],
          },
        },
      ],
    }));
    const out = loadState();
    const ex = out.signals[0].rules.exits[0];
    expect(ex.target_entry_block_names).toEqual(['MyEntry']);
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

  it('preserves target_entry_block_names on exit blocks', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [{ id: 'entry-1', input_id: 'X', weight: 50, conditions: [] }],
            exits: [{ id: 'exit-1', target_entry_block_names: ['MyEntry'], conditions: [] }],
          },
        },
      ],
    }));
    const out = loadState();
    const exit = out.signals[0].rules.exits[0];
    expect(exit.target_entry_block_names).toEqual(['MyEntry']);
    expect('target_entry_block_name' in exit).toBe(false);
  });

  it('folds a stray legacy singular target_entry_block_name into the plural array (belt-and-braces)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      // Already v6, but a malformed payload still carries the singular key.
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [{ id: 'entry-1', input_id: 'X', weight: 50, conditions: [] }],
            exits: [{ id: 'exit-1', target_entry_block_name: 'MyEntry', conditions: [] }],
          },
        },
      ],
    }));
    const out = loadState();
    const exit = out.signals[0].rules.exits[0];
    expect(exit.target_entry_block_names).toEqual(['MyEntry']);
    expect('target_entry_block_name' in exit).toBe(false);
  });

  it('defaults a missing target_entry_block_names on an exit block to []', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [],
            exits: [{ id: 'exit-1', conditions: [] }],
          },
        },
      ],
    }));
    const out = loadState();
    expect(out.signals[0].rules.exits[0].target_entry_block_names).toEqual([]);
  });

  it('de-duplicates repeated names in target_entry_block_names (order preserved)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [
        {
          id: 's1', name: 's1', inputs: [],
          rules: {
            entries: [],
            exits: [{ id: 'exit-1', target_entry_block_names: ['A', 'B', 'A', '', 'B'], conditions: [] }],
          },
        },
      ],
    }));
    const out = loadState();
    expect(out.signals[0].rules.exits[0].target_entry_block_names).toEqual(['A', 'B']);
  });

  it('does NOT add target_entry_block_names to entry blocks', () => {
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
    expect('target_entry_block_name' in entry).toBe(false);
    expect('target_entry_block_names' in entry).toBe(false);
  });

  it('sanitiser strips legacy target_entry_block_id on exits and preserves target_entry_block_names', () => {
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
                target_entry_block_id: 'old-entry-id',          // legacy — must be stripped
                target_entry_block_names: ['Momentum'],          // new field — must survive
                conditions: [],
              },
            ],
          },
        },
      ],
    }));
    const out = loadState();
    const ex = out.signals[0].rules.exits[0];
    expect(ex.target_entry_block_names).toEqual(['Momentum']);
    expect('target_entry_block_id' in ex).toBe(false);
  });

  it('hydrates enabled to true when field is missing from a block', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 's1', inputs: [],
        rules: {
          entries: [{ id: 'b1', input_id: 'X', weight: 50, conditions: [] }],
          exits: [],
        },
      }],
    }));
    const block = loadState().signals[0].rules.entries[0];
    expect(block.enabled).toBe(true);
  });

  it('preserves enabled=false when explicitly stored', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 's1', inputs: [],
        rules: {
          entries: [{ id: 'b1', input_id: 'X', weight: 50, conditions: [], enabled: false }],
          exits: [],
        },
      }],
    }));
    const block = loadState().signals[0].rules.entries[0];
    expect(block.enabled).toBe(false);
  });

  it('hydrates description to "" when field is missing from a block', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 's1', inputs: [],
        rules: {
          entries: [{ id: 'b1', input_id: 'X', weight: 50, conditions: [] }],
          exits: [],
        },
      }],
    }));
    const block = loadState().signals[0].rules.entries[0];
    expect(block.description).toBe('');
  });

  it('preserves a non-empty description string when stored', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 's1', inputs: [],
        rules: {
          entries: [{ id: 'b1', input_id: 'X', weight: 50, conditions: [], description: 'Fires on RSI dip' }],
          exits: [],
        },
      }],
    }));
    const block = loadState().signals[0].rules.entries[0];
    expect(block.description).toBe('Fires on RSI dip');
  });

  it('hydrates enabled and description on exit blocks too', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 's1', inputs: [],
        rules: {
          entries: [],
          exits: [{ id: 'x1', target_entry_block_names: ['Alpha'], conditions: [] }],
        },
      }],
    }));
    const exit = loadState().signals[0].rules.exits[0];
    expect(exit.enabled).toBe(true);
    expect(exit.description).toBe('');
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

// ---------------------------------------------------------------------------
// v5 → v6 migration: singular target_entry_block_name → plural
// target_entry_block_names. The localStorage KEY is unchanged
// (tcg.signals.v5); only the in-payload version field flips 5 → 6 and exit
// blocks gain the plural array. Existing v5 signals must load, run, and
// round-trip.
// ---------------------------------------------------------------------------
describe('migrateV5ToV6 (pure)', () => {
  it('folds a non-empty singular name into a one-element plural array', () => {
    const v5 = {
      version: 5,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [{ id: 'e1', input_id: 'X', weight: 50, name: 'Alpha', conditions: [] }],
          exits: [{ id: 'x1', target_entry_block_name: 'Alpha', conditions: [] }],
        },
      }],
    };
    const out = migrateV5ToV6(v5);
    expect(out.version).toBe(6);
    const ex = out.signals[0].rules.exits[0];
    expect(ex.target_entry_block_names).toEqual(['Alpha']);
    expect('target_entry_block_name' in ex).toBe(false);
  });

  it('folds an empty singular name into an empty array', () => {
    const v5 = {
      version: 5,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: { entries: [], exits: [{ id: 'x1', target_entry_block_name: '', conditions: [] }] },
      }],
    };
    const ex = migrateV5ToV6(v5).signals[0].rules.exits[0];
    expect(ex.target_entry_block_names).toEqual([]);
    expect('target_entry_block_name' in ex).toBe(false);
  });

  it('does not mutate the input payload', () => {
    const v5 = {
      version: 5,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: { entries: [], exits: [{ id: 'x1', target_entry_block_name: 'Alpha', conditions: [] }] },
      }],
    };
    migrateV5ToV6(v5);
    expect(v5.version).toBe(5);
    expect(v5.signals[0].rules.exits[0].target_entry_block_name).toBe('Alpha');
  });
});

describe('loadState — v5 payload is migrated to v6 (not dropped)', () => {
  it('loads a v5 signal with a singular exit target as a plural array', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: 5,
      signals: [{
        id: 's1', name: 'Legacy', doc: '', inputs: [],
        rules: {
          entries: [{ id: 'e1', input_id: 'X', weight: 50, name: 'Alpha', conditions: [] }],
          exits: [{ id: 'x1', name: '', target_entry_block_name: 'Alpha', conditions: [] }],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const out = loadState();
    expect(out.signals).toHaveLength(1);
    const ex = out.signals[0].rules.exits[0];
    expect(ex.target_entry_block_names).toEqual(['Alpha']);
    expect('target_entry_block_name' in ex).toBe(false);
    // No incompatible-version warning for a v5 payload — it migrates.
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('a migrated v5 signal round-trips: load → save → load is stable', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: 5,
      signals: [{
        id: 's1', name: 'Legacy', doc: '', inputs: [],
        rules: {
          entries: [{ id: 'e1', input_id: 'X', weight: 50, name: 'Alpha', conditions: [] }],
          exits: [{ id: 'x1', name: '', target_entry_block_name: 'Alpha', conditions: [] }],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const first = loadState();
    saveState(first);
    const second = loadState();
    expect(second).toEqual(first);
    expect(second.signals[0].rules.exits[0].target_entry_block_names).toEqual(['Alpha']);
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
        { id: 'e1', input_id: 'X', weight: 50, name: 'Alpha', conditions: [] },
        { id: 'e2', input_id: 'X', weight: -30, name: 'Beta', conditions: [] },
      ],
      exits: [
        { id: 'x1', target_entry_block_names: ['Alpha'], conditions: [] },
        { id: 'x2', target_entry_block_names: ['Beta'], conditions: [] },
        { id: 'x3', target_entry_block_names: ['Alpha'], conditions: [] },
      ],
    },
    settings: { dont_repeat: true },
  };

  it('removes the entry and every exit whose ONLY target was its name', () => {
    const next = cascadeDeleteEntry(sig, 'e1');
    expect(next.rules.entries.map((b) => b.id)).toEqual(['e2']);
    // x1 + x3 targeted only Alpha → removed; x2 (Beta) survives.
    expect(next.rules.exits.map((b) => b.id)).toEqual(['x2']);
  });
  it('leaves untouched entries and unrelated exits alone', () => {
    const next = cascadeDeleteEntry(sig, 'e2');
    expect(next.rules.entries.map((b) => b.id)).toEqual(['e1']);
    expect(next.rules.exits.map((b) => b.id)).toEqual(['x1', 'x3']);
  });
  it('v6: strips the deleted name but KEEPS an exit that still targets another entry', () => {
    const sigMulti = {
      ...sig,
      rules: {
        entries: sig.rules.entries,
        // x1 targets BOTH Alpha and Beta; deleting Alpha must keep x1 (now [Beta]).
        exits: [
          { id: 'x1', target_entry_block_names: ['Alpha', 'Beta'], conditions: [] },
          { id: 'x2', target_entry_block_names: ['Alpha'], conditions: [] },
        ],
      },
    };
    const next = cascadeDeleteEntry(sigMulti, 'e1'); // deletes name 'Alpha'
    expect(next.rules.exits.map((b) => b.id)).toEqual(['x1']);
    expect(next.rules.exits[0].target_entry_block_names).toEqual(['Beta']);
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
  it('does not cascade exits when deleted entry has no name', () => {
    const sigNoName = {
      ...sig,
      rules: {
        entries: [
          { id: 'e1', input_id: 'X', weight: 50, name: '', conditions: [] },
          { id: 'e2', input_id: 'X', weight: -30, name: 'Beta', conditions: [] },
        ],
        exits: [
          { id: 'x1', target_entry_block_names: [], conditions: [] },
          { id: 'x2', target_entry_block_names: ['Beta'], conditions: [] },
        ],
      },
    };
    const next = cascadeDeleteEntry(sigNoName, 'e1');
    // Entry removed, but exits untouched because deleted name is empty
    expect(next.rules.entries.map((b) => b.id)).toEqual(['e2']);
    expect(next.rules.exits).toHaveLength(2);
  });
  // Regression: cascadeDeleteEntry must preserve all other rules sections
  // (notably rules.resets), otherwise the next autosave round-trips them
  // through sanitiseSignal and silently drops user data.
  it('preserves rules.resets and any other rules.* section unchanged', () => {
    const r1 = { id: 'r1', name: 'Arm-A', conditions: [], enabled: true, description: '' };
    const r2 = { id: 'r2', name: 'Arm-B', conditions: [], enabled: true, description: '' };
    const sigWithResets = {
      ...sig,
      rules: {
        entries: sig.rules.entries,
        exits: sig.rules.exits,
        resets: [r1, r2],
      },
    };
    const next = cascadeDeleteEntry(sigWithResets, 'e1');
    // Existing cascade behaviour still holds.
    expect(next.rules.entries.map((b) => b.id)).toEqual(['e2']);
    expect(next.rules.exits.map((b) => b.id)).toEqual(['x2']);
    // The bug under test: resets must survive untouched.
    expect(next.rules.resets).toEqual([r1, r2]);
  });

  // Cascade-delete must preserve requires_reset_block_id on SURVIVING
  // entries/exits — otherwise deleting any entry silently strips bindings.
  it('preserves requires_reset_block_id on surviving entries and exits', () => {
    const r1 = { id: 'r1', name: 'Arm', conditions: [], enabled: true, description: '' };
    const survivingEntry = {
      ...sig.rules.entries[1],
      requires_reset_block_id: 'r1',
    };
    const survivingExit = {
      ...sig.rules.exits[1],
      requires_reset_block_id: 'r1',
    };
    const sigBound = {
      ...sig,
      rules: {
        entries: [sig.rules.entries[0], survivingEntry],
        exits: [sig.rules.exits[0], survivingExit],
        resets: [r1],
      },
    };
    const next = cascadeDeleteEntry(sigBound, 'e1');
    const e2 = next.rules.entries.find((b) => b.id === 'e2');
    const x2 = next.rules.exits.find((b) => b.id === 'x2');
    expect(e2.requires_reset_block_id).toBe('r1');
    expect(x2.requires_reset_block_id).toBe('r1');
  });
});

describe('Reset blocks — soft-migration + sanitiser', () => {
  // T15
  it('round-trips a v5 signal carrying rules.resets', () => {
    const sig = {
      id: 's1',
      name: 'Reset signal',
      doc: '',
      inputs: [
        { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: 'SPX' } },
      ],
      rules: {
        entries: [],
        exits: [],
        resets: [
          {
            id: 'r1',
            name: 'Arm',
            conditions: [
              {
                op: 'gt',
                lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
                rhs: { kind: 'constant', value: 100 },
              },
            ],
            enabled: true,
            description: '',
          },
        ],
      },
      settings: { dont_repeat: true },
    };
    saveState({ signals: [sig] });
    const loaded = loadState();
    expect(loaded.signals).toHaveLength(1);
    expect(loaded.signals[0].rules.resets).toEqual(sig.rules.resets);
  });

  it('sanitiseBlock strips disallowed fields on reset blocks', () => {
    const dirty = {
      id: 's1', name: '', doc: '',
      inputs: [],
      rules: {
        entries: [],
        exits: [],
        resets: [
          {
            id: 'r1',
            name: 'Arm',
            input_id: 'X',           // disallowed — must be stripped
            weight: 42,              // disallowed — must be stripped
            target_entry_block_name: 'something',  // disallowed — must be stripped
            conditions: [],
            enabled: true,
            description: '',
          },
        ],
      },
      settings: { dont_repeat: true },
    };
    saveState({ signals: [dirty] });
    const loaded = loadState();
    const reset = loaded.signals[0].rules.resets[0];
    expect(reset.id).toBe('r1');
    expect(reset.name).toBe('Arm');
    expect('input_id' in reset).toBe(false);
    expect('weight' in reset).toBe(false);
    expect('target_entry_block_name' in reset).toBe(false);
    expect('target_entry_block_names' in reset).toBe(false);
  });

  // T19 — legacy v5 payload without `resets` loads with `resets: []`
  it('legacy v5 payload without rules.resets loads with resets: []', () => {
    const legacy = {
      version: 5,
      signals: [
        {
          id: 's1',
          name: 'Legacy',
          doc: '',
          inputs: [],
          rules: {
            entries: [],
            exits: [],
            // no resets field
          },
          settings: { dont_repeat: true },
        },
      ],
    };
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify(legacy));
    const loaded = loadState();
    expect(loaded.signals).toHaveLength(1);
    expect(loaded.signals[0].rules.resets).toEqual([]);
  });
});

// Per CONTRACT §6.1 — per-block requires_reset_block_id round-trips on
// entries+exits, defaults null when missing, and is STRIPPED from resets.
describe('Signals storage — per-block requires_reset_block_id (v5)', () => {
  it('round-trips requires_reset_block_id on entry blocks', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [{
            id: 'e1', input_id: 'X', weight: 10, name: '', conditions: [],
            enabled: true, description: '',
            requires_reset_block_id: 'reset-uuid-7',
          }],
          exits: [],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const out = loadState();
    expect(out.signals[0].rules.entries[0].requires_reset_block_id).toBe('reset-uuid-7');
  });

  it('round-trips requires_reset_block_id on exit blocks', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [],
          exits: [{
            id: 'x1', name: '', target_entry_block_names: ['Alpha'],
            conditions: [], enabled: true, description: '',
            requires_reset_block_id: 'reset-uuid-9',
          }],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const out = loadState();
    expect(out.signals[0].rules.exits[0].requires_reset_block_id).toBe('reset-uuid-9');
  });

  it('missing requires_reset_block_id defaults to null on entries+exits', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [{
            id: 'e1', input_id: 'X', weight: 10, name: '', conditions: [],
            enabled: true, description: '',
            // requires_reset_block_id omitted
          }],
          exits: [{
            id: 'x1', name: '', target_entry_block_names: ['Alpha'],
            conditions: [], enabled: true, description: '',
            // requires_reset_block_id omitted
          }],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const out = loadState();
    expect(out.signals[0].rules.entries[0].requires_reset_block_id).toBe(null);
    expect(out.signals[0].rules.exits[0].requires_reset_block_id).toBe(null);
  });

  it('strips requires_reset_block_id from reset blocks (Sign 4)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [],
          exits: [],
          resets: [{
            id: 'r1', name: '', conditions: [], enabled: true, description: '',
            // Tampered legacy payload — must be stripped on load.
            requires_reset_block_id: 'should-not-survive',
          }],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const out = loadState();
    expect('requires_reset_block_id' in out.signals[0].rules.resets[0]).toBe(false);
  });
});

// coerceResetCount — the ONE shared int≥1 coercion used by storage,
// requestBuilder, and BlockHeader. Pinned here (its home module) plus a
// cross-module identity check so the three call sites can never drift the
// way they did before (Number(x) vs parseFloat(x) diverging on "3px"/"" ).
describe('coerceResetCount — shared reset-count coercion (int ≥ 1, default 1)', () => {
  // raw input -> expected. invalid / non-finite / < 1 -> 1; valid -> floor.
  const CASES = [
    ['3px', 1], // Number('3px') === NaN -> 1 (parseFloat would have said 3 — the OLD divergence)
    ['', 1], // Number('') === 0 -> <1 -> 1 (parseFloat would have said NaN -> also 1, but via a different path)
    [2.9, 2], // floor
    [0, 1], // <1
    [-1, 1], // <1
    [Number.NaN, 1], // non-finite
    [3, 3], // valid integer
    ['2', 2], // numeric string
  ];

  it.each(CASES)('coerces %o -> %i', (raw, expected) => {
    expect(coerceResetCount(raw)).toBe(expected);
  });

  it.each(CASES)('storage / requestBuilder / blockShape agree on %o', (raw) => {
    const fromStorage = coerceResetCount(raw);
    expect(coerceFromBlockShape(raw)).toBe(fromStorage);
    expect(coerceFromRequest(raw)).toBe(fromStorage);
  });

  it('all three references point at the SAME function object', () => {
    // Strongest possible guarantee they cannot diverge: identity, not just
    // value-equality. One helper, imported everywhere.
    expect(coerceFromBlockShape).toBe(coerceResetCount);
    expect(coerceFromRequest).toBe(coerceResetCount);
  });
});

// Orphan-count-on-store: a block with NO reset bound must never carry a
// stored count other than 1. sanitiseBlock forces it so a stale count
// (e.g. left over after the user cleared the binding) can't ride in storage.
describe('Signals storage — orphan requires_reset_count is forced to 1', () => {
  it('kills an orphan count on an entry block (no requires_reset_block_id)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [{
            id: 'e1', input_id: 'X', weight: 10, name: '', conditions: [],
            enabled: true, description: '',
            requires_reset_block_id: null, // no binding…
            requires_reset_count: 5, // …but a stored count -> orphan
          }],
          exits: [],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    expect(loadState().signals[0].rules.entries[0].requires_reset_count).toBe(1);
  });

  it('kills an orphan count on an exit block (no requires_reset_block_id)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [],
          exits: [{
            id: 'x1', name: '', target_entry_block_names: ['Alpha'],
            conditions: [], enabled: true, description: '',
            requires_reset_block_id: null, // no binding…
            requires_reset_count: 5, // …but a stored count -> orphan
          }],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    expect(loadState().signals[0].rules.exits[0].requires_reset_count).toBe(1);
  });

  it('keeps the count when a reset IS bound (not an orphan)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [{
            id: 'e1', input_id: 'X', weight: 10, name: '', conditions: [],
            enabled: true, description: '',
            requires_reset_block_id: 'r1', requires_reset_count: 5,
          }],
          exits: [],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    expect(loadState().signals[0].rules.entries[0].requires_reset_count).toBe(5);
  });
});

// requires_reset_count — sanitiser coerces to an integer ≥ 1 (default 1
// when absent/invalid) on entries+exits, round-trips a valid count, and
// strips the field from reset blocks. Crucially: WITHOUT a sanitiser
// clause the field is stripped on every load/save, so these tests guard
// against silent data loss. No SCHEMA_VERSION bump — the defaulting gives
// forward-compat for existing v5 signals that predate the field.
describe('Signals storage — per-block requires_reset_count (v5)', () => {
  it('round-trips a valid requires_reset_count on entry blocks', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [{
            id: 'e1', input_id: 'X', weight: 10, name: '', conditions: [],
            enabled: true, description: '',
            requires_reset_block_id: 'reset-uuid-7', requires_reset_count: 4,
          }],
          exits: [],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const out = loadState();
    expect(out.signals[0].rules.entries[0].requires_reset_count).toBe(4);
  });

  it('round-trips a valid requires_reset_count on exit blocks', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [],
          exits: [{
            id: 'x1', name: '', target_entry_block_names: ['Alpha'],
            conditions: [], enabled: true, description: '',
            requires_reset_block_id: 'reset-uuid-9', requires_reset_count: 6,
          }],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const out = loadState();
    expect(out.signals[0].rules.exits[0].requires_reset_count).toBe(6);
  });

  it('defaults a missing requires_reset_count to 1 on entries+exits (backward-compat)', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [{
            id: 'e1', input_id: 'X', weight: 10, name: '', conditions: [],
            enabled: true, description: '', requires_reset_block_id: 'r1',
            // requires_reset_count omitted — a pre-feature v5 signal
          }],
          exits: [{
            id: 'x1', name: '', target_entry_block_names: ['Alpha'],
            conditions: [], enabled: true, description: '',
            // requires_reset_count omitted
          }],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const out = loadState();
    expect(out.signals[0].rules.entries[0].requires_reset_count).toBe(1);
    expect(out.signals[0].rules.exits[0].requires_reset_count).toBe(1);
  });

  it('clamps an out-of-range or non-integer requires_reset_count to a valid integer ≥ 1', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          // Each carries a reset binding so the COERCION path is exercised
          // (an unbound block would be forced to 1 by the orphan-kill rule,
          // masking the floor/clamp behavior under test).
          entries: [
            { id: 'a', input_id: '', weight: 0, conditions: [], requires_reset_block_id: 'r1', requires_reset_count: 0 },
            { id: 'b', input_id: '', weight: 0, conditions: [], requires_reset_block_id: 'r1', requires_reset_count: -3 },
            { id: 'c', input_id: '', weight: 0, conditions: [], requires_reset_block_id: 'r1', requires_reset_count: 2.7 },
            { id: 'd', input_id: '', weight: 0, conditions: [], requires_reset_block_id: 'r1', requires_reset_count: 'nope' },
            { id: 'e', input_id: '', weight: 0, conditions: [], requires_reset_block_id: 'r1', requires_reset_count: Number.NaN },
          ],
          exits: [],
          resets: [],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const counts = loadState().signals[0].rules.entries.map((b) => b.requires_reset_count);
    // 0 → 1, -3 → 1, 2.7 → 2 (floor), 'nope' → 1, NaN → 1
    expect(counts).toEqual([1, 1, 2, 1, 1]);
    expect(counts.every(Number.isInteger)).toBe(true);
  });

  it('strips requires_reset_count from reset blocks', () => {
    storage.setItem(SIGNALS_STORAGE_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      signals: [{
        id: 's1', name: 'S1', inputs: [],
        rules: {
          entries: [],
          exits: [],
          resets: [{
            id: 'r1', name: '', conditions: [], enabled: true, description: '',
            // Tampered legacy payload — must be stripped on load.
            requires_reset_count: 9,
          }],
        },
        settings: { dont_repeat: true },
      }],
    }));
    const out = loadState();
    expect('requires_reset_count' in out.signals[0].rules.resets[0]).toBe(false);
  });
});
