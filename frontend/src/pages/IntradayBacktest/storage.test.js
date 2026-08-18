// @vitest-environment jsdom
import {
  describe, it, expect, beforeEach, afterEach, vi,
} from 'vitest';
import {
  INTRADAY_SIMS_KEY,
  SCHEMA_VERSION,
  DEFAULT_FORM,
  listSims,
  saveSim,
  loadSim,
  deleteSim,
  serializeConfig,
  deserializeConfig,
  deepEqual,
  __resetIncompatibleVersionWarnedForTests,
} from './storage';
import { defaultEntryModule, defaultExitModule } from './EntryExitModule';
import { defaultHedgeModule } from './HedgeModule';

// A realistic, non-default config so a round-trip proves fidelity (not just
// that defaults survive). Custom day carries an override + expanded flag; entry
// carries a condition with a React ``_id``.
function sampleState() {
  return {
    form: {
      start_date: '2025-02-10',
      end_date: '2025-03-31',
      expiry_mode: 'NDTE',
      dte: 3,
      straddle_side: 'short',
    },
    entry: {
      time: '10:15',
      snap_tolerance_minutes: 8,
      conditions: [{ _id: 'c1', type: 'min_premium', points: 0.75 }],
    },
    exit: { time: '15:30', snap_tolerance_minutes: 12, conditions: [], triggers: [] },
    hedge: defaultHedgeModule(),
    customDays: [
      {
        date: '2025-02-17', exclude: true, expanded: false, entry: null, exit: null,
      },
      {
        date: '2025-02-14',
        exclude: false,
        expanded: true,
        entry: { time: '11:00', snap_tolerance_minutes: '', conditions: [] },
        exit: null,
      },
    ],
  };
}

beforeEach(() => {
  window.localStorage.clear();
  __resetIncompatibleVersionWarnedForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('IntradayBacktest storage', () => {
  it('round-trips a config: saveSim → listSims → loadSim returns an identical config', () => {
    const state = sampleState();
    const saved = saveSim('My sim', state);

    expect(saved.id).toBeTruthy();
    expect(saved.name).toBe('My sim');
    expect(saved.savedAt).toBeTruthy();

    const list = listSims();
    expect(list.length).toBe(1);
    expect(list[0].name).toBe('My sim');

    const loaded = loadSim(saved.id);
    expect(loaded).not.toBeNull();
    // The stored/loaded config must equal the canonical serialization of the
    // input state (deep-equal, order-independent).
    expect(deepEqual(loaded.config, serializeConfig(state))).toBe(true);
    // Spot-check exact restoration of representative fields.
    expect(loaded.config.form.start_date).toBe('2025-02-10');
    expect(loaded.config.form.straddle_side).toBe('short');
    expect(loaded.config.form.dte).toBe(3);
    expect(loaded.config.entry.conditions[0]).toEqual({ _id: 'c1', type: 'min_premium', points: 0.75 });
    expect(loaded.config.customDays.find((c) => c.date === '2025-02-17').exclude).toBe(true);
    expect(loaded.config.customDays.find((c) => c.date === '2025-02-14').expanded).toBe(true);
  });

  it('persists across a fresh read (survives a simulated reload)', () => {
    const state = sampleState();
    const saved = saveSim('Persisted', state);
    // A brand-new read (no in-memory retention) still finds it.
    const again = listSims();
    expect(again.map((s) => s.id)).toContain(saved.id);
    expect(deepEqual(loadSim(saved.id).config, serializeConfig(state))).toBe(true);
  });

  it('deleteSim removes the entry', () => {
    const a = saveSim('A', sampleState());
    const b = saveSim('B', sampleState());
    expect(listSims().length).toBe(2);

    const removed = deleteSim(a.id);
    expect(removed).toBe(true);
    const list = listSims();
    expect(list.length).toBe(1);
    expect(list[0].id).toBe(b.id);
    // Deleting a non-existent id is a no-op returning false.
    expect(deleteSim('nope')).toBe(false);
  });

  it('saving the same name overwrites in place (no duplicate, id preserved)', () => {
    const first = saveSim('Dup', sampleState());
    const changed = { ...sampleState(), form: { ...sampleState().form, straddle_side: 'long' } };
    const second = saveSim('Dup', changed);

    const list = listSims();
    expect(list.length).toBe(1); // no duplicate row
    expect(second.id).toBe(first.id); // id + position preserved
    expect(loadSim(first.id).config.form.straddle_side).toBe('long'); // content updated
  });

  it('is sandbox/quota safe: getStorage null → no throw, empty list', () => {
    vi.stubGlobal('localStorage', undefined);
    expect(() => listSims()).not.toThrow();
    expect(listSims()).toEqual([]);
    // saveSim still returns a well-formed entry (best-effort, just not persisted).
    let entry;
    expect(() => { entry = saveSim('X', sampleState()); }).not.toThrow();
    expect(entry.name).toBe('X');
    expect(() => loadSim('whatever')).not.toThrow();
    expect(() => deleteSim('whatever')).not.toThrow();
  });

  it('drops an incompatible version payload on load', () => {
    window.localStorage.setItem(
      INTRADAY_SIMS_KEY,
      JSON.stringify({ version: 999, sims: [{ id: 'x', name: 'old', config: {} }] }),
    );
    expect(listSims()).toEqual([]);
  });

  it('drops junk entries and unparseable payloads; sanitises malformed config', () => {
    // Unparseable blob → empty.
    window.localStorage.setItem(INTRADAY_SIMS_KEY, '{not json');
    expect(listSims()).toEqual([]);

    // A mix: one valid sim + junk (missing id, non-object). Junk dropped, and
    // the valid sim's malformed config is sanitised to canonical defaults.
    window.localStorage.setItem(INTRADAY_SIMS_KEY, JSON.stringify({
      version: SCHEMA_VERSION,
      sims: [
        { name: 'no-id', config: {} }, // dropped (no id)
        42, // dropped (not an object)
        { id: 'ok', name: 'valid', config: { form: 'garbage', entry: 5, customDays: 'x' } },
      ],
    }));
    const list = listSims();
    expect(list.length).toBe(1);
    expect(list[0].id).toBe('ok');
    // Malformed fields collapsed to canonical defaults.
    expect(list[0].config.form).toEqual({ ...DEFAULT_FORM });
    expect(list[0].config.entry).toEqual(defaultEntryModule());
    expect(list[0].config.exit).toEqual(defaultExitModule());
    expect(list[0].config.customDays).toEqual([]);
  });

  it('serializeConfig / deserializeConfig are a stable, idempotent boundary', () => {
    const cfg = serializeConfig(sampleState());
    // Idempotent: re-serializing the canonical form is a fixed point.
    expect(deepEqual(serializeConfig(cfg), cfg)).toBe(true);
    // deserializeConfig accepts a bare config OR a wrapped {config} and yields
    // the same canonical shape.
    expect(deepEqual(deserializeConfig(cfg), cfg)).toBe(true);
    expect(deepEqual(deserializeConfig({ config: cfg }), cfg)).toBe(true);
  });

  it('deepEqual is order-independent for object keys', () => {
    expect(deepEqual({ a: 1, b: 2 }, { b: 2, a: 1 })).toBe(true);
    expect(deepEqual({ a: 1 }, { a: 1, b: 2 })).toBe(false);
    expect(deepEqual([1, 2], [1, 2])).toBe(true);
    expect(deepEqual([1, 2], [2, 1])).toBe(false);
  });
});
