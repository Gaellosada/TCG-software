// Shared hydration for backend SignalOut payloads → editor-shape signal.
//
// Extracted from SignalsPage so other consumers (e.g. the Portfolio
// SignalPickerModal) hydrate the exact same way — single source of truth,
// no duplication. Backend field ``description`` maps to local ``doc``; the
// rest mirror, with rules/settings defaulted via the storage helpers.

import { emptyRules, defaultSettings } from './storage';

/**
 * Fold an exit block's legacy singular ``target_entry_block_name`` (string)
 * into the canonical plural ``target_entry_block_names`` (string[]).
 *
 * Why this lives here (M3): a signal SAVED BEFORE the v6 multi-target change
 * stores the singular key; the backend echoes it back verbatim. Without this
 * fold the editor (which only reads the plural array) drops the target
 * silently, and a re-save/compute then emits ``[]`` — the exit's link to its
 * entry vanishes. The localStorage load path already folds singular→plural
 * (storage.sanitiseTargetEntryNames); this mirrors that one rule for the
 * backend hydrate path, deliberately self-contained so hydration carries NO
 * other normalisation (no id regeneration / field stripping) — the editor
 * already owns those concerns. Plural wins if both keys are present; an
 * empty-string singular → ``[]``; the singular key is always dropped.
 *
 * @param {object} exit  one raw exit block from a backend doc.
 * @returns {object}     the exit block with a clean plural target array.
 */
function normaliseExitTargets(exit) {
  if (!exit || typeof exit !== 'object') return exit;
  const { target_entry_block_name: legacy, ...rest } = exit;
  let names;
  if (Array.isArray(exit.target_entry_block_names)) {
    names = exit.target_entry_block_names.filter((n) => typeof n === 'string' && n);
  } else if (typeof legacy === 'string' && legacy) {
    names = [legacy];
  } else {
    names = [];
  }
  return { ...rest, target_entry_block_names: names };
}

/**
 * Build the editor-shape signal object from a backend SignalOut payload.
 *
 * @param {object} persisted  Backend SignalOut payload.
 * @returns {{ id: string, name: string, inputs: Array<object>,
 *   rules: object, settings: object, doc: string, locked: boolean }}
 */
export function hydrateFromPersisted(persisted) {
  const inputs = Array.isArray(persisted.inputs) ? persisted.inputs : [];
  const merged = (persisted.rules && typeof persisted.rules === 'object')
    ? { ...emptyRules(), ...persisted.rules }
    : emptyRules();
  // Normalise exit-block targets (legacy singular → plural) so an exit saved
  // before the v6 multi-target change keeps its target through the editor.
  const rules = {
    ...merged,
    exits: Array.isArray(merged.exits) ? merged.exits.map(normaliseExitTargets) : [],
  };
  const settings = (persisted.settings && typeof persisted.settings === 'object')
    ? { ...defaultSettings(), ...persisted.settings }
    : defaultSettings();
  return {
    id: persisted.id,
    name: persisted.name || 'Untitled',
    inputs,
    rules,
    settings,
    doc: typeof persisted.description === 'string' ? persisted.description : '',
    // Lock state (defaults to false for older docs that predate the field).
    locked: persisted.locked === true,
  };
}
