// Shared hydration for backend SignalOut payloads → editor-shape signal.
//
// Extracted from SignalsPage so other consumers (e.g. the Portfolio
// SignalPickerModal) hydrate the exact same way — single source of truth,
// no duplication. Backend field ``description`` maps to local ``doc``; the
// rest mirror, with rules/settings defaulted via the storage helpers.

import { emptyRules, defaultSettings } from './storage';

/**
 * Build the editor-shape signal object from a backend SignalOut payload.
 *
 * @param {object} persisted  Backend SignalOut payload.
 * @returns {{ id: string, name: string, inputs: Array<object>,
 *   rules: object, settings: object, doc: string }}
 */
export function hydrateFromPersisted(persisted) {
  const inputs = Array.isArray(persisted.inputs) ? persisted.inputs : [];
  const rules = (persisted.rules && typeof persisted.rules === 'object')
    ? { ...emptyRules(), ...persisted.rules }
    : emptyRules();
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
  };
}
