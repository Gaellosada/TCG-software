import { describe, it, expect } from 'vitest';
import { hydrateFromPersisted } from './hydrateSignal';

// hydrateFromPersisted turns a backend SignalOut payload into the editor-shape
// signal. The critical correctness concern here is the LEGACY-MIGRATION gap
// (M3): a signal saved BEFORE the v6 multi-target change stores an exit block's
// target as the singular ``target_entry_block_name`` (string). The hydrate path
// must fold that into the plural ``target_entry_block_names`` (string[]) — the
// only shape the editor and the wire-builder understand — or the exit target
// silently vanishes and a re-save/compute emits [].

describe('hydrateFromPersisted — legacy exit-target migration (M3)', () => {
  it('folds a legacy singular target_entry_block_name into the plural array', () => {
    const persisted = {
      id: 'sig-1',
      name: 'Legacy signal',
      inputs: [{ id: 'in1', instrument: null }],
      rules: {
        entries: [
          { id: 'e1', name: 'Alpha', input_id: 'in1', weight: 50, conditions: [] },
        ],
        exits: [
          // Legacy doc: singular key only, NO plural array.
          {
            id: 'x1',
            name: 'Exit A',
            conditions: [],
            target_entry_block_name: 'Alpha',
          },
        ],
      },
    };

    const hydrated = hydrateFromPersisted(persisted);

    expect(hydrated.rules.exits).toHaveLength(1);
    expect(hydrated.rules.exits[0].target_entry_block_names).toEqual(['Alpha']);
    // The singular key must not survive into editor state.
    expect(hydrated.rules.exits[0]).not.toHaveProperty('target_entry_block_name');
  });

  it('drops an empty-string legacy singular to [] (not [""])', () => {
    const persisted = {
      id: 'sig-2',
      name: 'Empty legacy target',
      inputs: [],
      rules: {
        exits: [
          {
            id: 'x1', name: 'Exit', conditions: [], target_entry_block_name: '',
          },
        ],
      },
    };

    const hydrated = hydrateFromPersisted(persisted);

    expect(hydrated.rules.exits[0].target_entry_block_names).toEqual([]);
  });

  it('honours an existing plural array when both keys are present (plural wins)', () => {
    const persisted = {
      id: 'sig-3',
      name: 'Mixed',
      inputs: [],
      rules: {
        exits: [
          {
            id: 'x1',
            name: 'Exit',
            conditions: [],
            target_entry_block_name: 'Stale',
            target_entry_block_names: ['Alpha', 'Beta'],
          },
        ],
      },
    };

    const hydrated = hydrateFromPersisted(persisted);

    expect(hydrated.rules.exits[0].target_entry_block_names).toEqual(['Alpha', 'Beta']);
    expect(hydrated.rules.exits[0]).not.toHaveProperty('target_entry_block_name');
  });

  it('preserves a modern plural-only exit target unchanged', () => {
    const persisted = {
      id: 'sig-4',
      name: 'Modern',
      inputs: [],
      rules: {
        exits: [
          {
            id: 'x1', name: 'Exit', conditions: [], target_entry_block_names: ['Gamma'],
          },
        ],
      },
    };

    const hydrated = hydrateFromPersisted(persisted);

    expect(hydrated.rules.exits[0].target_entry_block_names).toEqual(['Gamma']);
  });
});

describe('hydrateFromPersisted — base mapping', () => {
  it('maps description→doc, defaults name, and reads lock state', () => {
    const hydrated = hydrateFromPersisted({
      id: 'sig-5',
      name: '',
      description: 'My notes',
      locked: true,
      inputs: [],
      rules: {},
    });

    expect(hydrated.doc).toBe('My notes');
    expect(hydrated.name).toBe('Untitled');
    expect(hydrated.locked).toBe(true);
    // Rules are always the canonical three-section shape.
    expect(hydrated.rules).toHaveProperty('entries');
    expect(hydrated.rules).toHaveProperty('exits');
    expect(hydrated.rules).toHaveProperty('resets');
  });

  it('defaults locked to false for older docs that predate the field', () => {
    const hydrated = hydrateFromPersisted({ id: 'sig-6', name: 'X', inputs: [], rules: {} });
    expect(hydrated.locked).toBe(false);
  });
});
