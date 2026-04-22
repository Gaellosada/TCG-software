// Local persistence for the Signals page state — schema v4.
//
// All direct ``localStorage`` access for signals lives in this module —
// other modules MUST go through load/save here.
//
// Schema v4 (signals-refactor-v4):
//   {
//     "version": 4,
//     "signals": [
//       {
//         "id", "name", "doc",
//         "inputs": [ { "id": "X", "instrument": InputInstrument } ],
//         "rules": {
//           "entries": [ Block ],
//           "exits":   [ Block ]
//         },
//         "settings": { "dont_repeat": true }
//       }
//     ]
//   }
//
// Entry Block = {
//   id: <uuid>,                      // stable, generated on creation
//   input_id: <string>,
//   weight: <float in [-100, +100]>, // SIGNED percentage; sign decides long/short
//   conditions: Condition[],         // unchanged from v3
// }
// Exit Block = {
//   id: <uuid>,                      // stable, generated on creation
//   target_entry_block_id: <uuid>,   // MUST match an existing entry block id
//   conditions: Condition[],
// }
// Exit blocks do NOT carry block-level input_id or weight; the
// operating input is derived from the target entry's input_id. The
// sanitiser strips any such legacy fields on load.
//
// InputInstrument is a discriminated union:
//   - Spot:        { type: 'spot',       collection, instrument_id }
//   - Continuous:  { type: 'continuous', collection, adjustment, cycle,
//                    rollOffset, strategy }
//
// Operand shapes (stored verbatim):
//   - indicator:   { kind:'indicator', indicator_id, input_id, output,
//                    params_override, series_override }
//   - instrument:  { kind:'instrument', input_id, field }
//   - constant:    { kind:'constant', value }
//
// Migration policy (v4): ANY payload with version !== 4 is DROPPED on load
// (single console.warn per page load). No v3→v4 or v2→drop code.

import { SIGNALS_STORAGE_KEY } from './storageKeys';

export const SCHEMA_VERSION = 4;

/** Canonical list of rule sections. */
export const SECTIONS = Object.freeze(['entries', 'exits']);

/** Max absolute percentage weight — no leverage. */
export const MAX_ABS_WEIGHT = 100;

/** Produce an empty rules map with both sections present. */
export function emptyRules() {
  return { entries: [], exits: [] };
}

/** Default settings applied to a brand-new signal. */
export function defaultSettings() {
  return { dont_repeat: true };
}

/** Alphabet used for auto-assigning input ids on creation. */
const INPUT_ID_ALPHABET = ['X', 'Y', 'Z', 'W', 'U', 'V', 'A', 'B', 'C', 'D'];

/**
 * Return the next free single-letter id given an existing input array.
 * Skips any id already used (case-sensitive). Falls back to "I<n>" if
 * the alphabet is exhausted.
 */
export function nextInputId(existing) {
  const taken = new Set((existing || []).map((i) => (i && typeof i.id === 'string') ? i.id : ''));
  for (const letter of INPUT_ID_ALPHABET) {
    if (!taken.has(letter)) return letter;
  }
  let n = 1;
  while (taken.has(`I${n}`)) n += 1;
  return `I${n}`;
}

/** Generate a stable block id. Uses crypto.randomUUID when available. */
export function newBlockId() {
  try {
    if (typeof globalThis !== 'undefined'
        && globalThis.crypto
        && typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID();
    }
  } catch {
    // fall through to fallback
  }
  return `blk-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function getStorage() {
  try {
    if (typeof globalThis !== 'undefined' && globalThis.localStorage) {
      return globalThis.localStorage;
    }
  } catch {
    // sandbox
  }
  return null;
}

let incompatibleVersionWarned = false;

export function __resetIncompatibleVersionWarnedForTests() {
  incompatibleVersionWarned = false;
}

function sanitiseSpotInstrument(raw) {
  const collection = typeof raw.collection === 'string' ? raw.collection : '';
  const instrument_id = typeof raw.instrument_id === 'string' ? raw.instrument_id : '';
  if (!collection || !instrument_id) return null;
  return { type: 'spot', collection, instrument_id };
}

function sanitiseContinuousInstrument(raw) {
  const collection = typeof raw.collection === 'string' ? raw.collection : '';
  if (!collection) return null;
  const adjustment = ['none', 'proportional', 'difference'].includes(raw.adjustment)
    ? raw.adjustment : 'none';
  const cycle = (typeof raw.cycle === 'string' && raw.cycle) ? raw.cycle : null;
  const rollOffset = Number.isFinite(raw.rollOffset) ? raw.rollOffset : 0;
  // Only one strategy is supported today — the sanitiser hard-codes it so
  // a tampered payload can't smuggle in a rogue value.
  return { type: 'continuous', collection, adjustment, cycle, rollOffset, strategy: 'front_month' };
}

function sanitiseInstrument(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.type === 'continuous') return sanitiseContinuousInstrument(raw);
  // Default/legacy path: treat as spot.
  return sanitiseSpotInstrument(raw);
}

function sanitiseInput(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const id = typeof raw.id === 'string' ? raw.id : '';
  if (!id) return null;
  const instrument = raw.instrument ? sanitiseInstrument(raw.instrument) : null;
  // Inputs may exist without a fully-configured instrument (user is still
  // picking) — we keep them but signal via instrument=null. The Run-gate
  // checks configuration before submitting.
  return { id, instrument };
}

/**
 * Sanitise a weight value.
 *   - Non-finite → 0.
 *   - Clamped to [-MAX_ABS_WEIGHT, +MAX_ABS_WEIGHT] (no leverage).
 *   - Sign preserved.
 */
function sanitiseWeight(raw) {
  const n = typeof raw === 'number' ? raw : Number(raw);
  if (!Number.isFinite(n)) return 0;
  if (n > MAX_ABS_WEIGHT) return MAX_ABS_WEIGHT;
  if (n < -MAX_ABS_WEIGHT) return -MAX_ABS_WEIGHT;
  return n;
}

function sanitiseBlock(raw, section) {
  const id = (typeof raw.id === 'string' && raw.id) ? raw.id : newBlockId();
  const name = typeof raw.name === 'string' ? raw.name : '';
  const conditions = Array.isArray(raw.conditions)
    ? raw.conditions.filter((c) => c && typeof c === 'object' && typeof c.op === 'string')
    : [];
  if (section === 'exits') {
    // Exit blocks carry no block-level input_id or weight — the
    // operating input is derived from the target entry. Legacy
    // payloads may include these fields; strip them.
    return {
      id,
      name,
      conditions,
      target_entry_block_id: typeof raw.target_entry_block_id === 'string'
        ? raw.target_entry_block_id
        : '',
    };
  }
  const input_id = typeof raw.input_id === 'string' ? raw.input_id : '';
  const weight = sanitiseWeight(raw.weight);
  return { id, input_id, weight, name, conditions };
}

function sanitiseSettings(raw) {
  const out = defaultSettings();
  if (raw && typeof raw === 'object') {
    // Preserve stored value if explicitly present (honouring user's prior
    // choice on v4-saved signals). Default applies only when absent.
    if (typeof raw.dont_repeat === 'boolean') {
      out.dont_repeat = raw.dont_repeat;
    }
  }
  return out;
}

function sanitiseSignal(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (typeof raw.id !== 'string' || !raw.id) return null;
  const name = typeof raw.name === 'string' && raw.name ? raw.name : 'Untitled';
  const rawInputs = Array.isArray(raw.inputs) ? raw.inputs : [];
  const inputs = rawInputs.map(sanitiseInput).filter((x) => x !== null);
  const rules = emptyRules();
  const rawRules = (raw.rules && typeof raw.rules === 'object') ? raw.rules : {};
  for (const section of SECTIONS) {
    const blocks = Array.isArray(rawRules[section]) ? rawRules[section] : [];
    rules[section] = blocks
      .filter((b) => b && typeof b === 'object')
      .map((b) => sanitiseBlock(b, section));
  }
  const settings = sanitiseSettings(raw.settings);
  const doc = typeof raw.doc === 'string' ? raw.doc : '';
  return { id: raw.id, name, inputs, rules, settings, doc };
}

export function loadState() {
  const empty = { signals: [] };
  const ls = getStorage();
  if (!ls) return empty;
  let raw;
  try {
    raw = ls.getItem(SIGNALS_STORAGE_KEY);
  } catch {
    return empty;
  }
  if (!raw) return empty;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return empty;
  }
  if (!parsed || typeof parsed !== 'object') return empty;
  if (parsed.version !== SCHEMA_VERSION) {
    if (!incompatibleVersionWarned) {
      incompatibleVersionWarned = true;
      // eslint-disable-next-line no-console
      console.warn('[signals] discarding incompatible v' + parsed.version + ' state');
    }
    return empty;
  }

  const rawSignals = Array.isArray(parsed.signals) ? parsed.signals : [];
  const signals = [];
  for (const s of rawSignals) {
    const cleaned = sanitiseSignal(s);
    if (cleaned) signals.push(cleaned);
  }
  return { signals };
}

export function saveState(state) {
  const ls = getStorage();
  if (!ls) return;
  const signals = Array.isArray(state?.signals) ? state.signals : [];
  const payload = {
    version: SCHEMA_VERSION,
    signals: signals
      .map(sanitiseSignal)
      .filter((s) => s !== null),
  };
  try {
    ls.setItem(SIGNALS_STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // quota
  }
}

/**
 * Return a new signal with the entry block ``entryId`` removed, and every
 * exit referencing that entry also removed (cascade delete per PLAN.md).
 * Pure — does not mutate the input.
 *
 * If ``entryId`` does not match any existing entry, the signal is
 * returned unchanged (structurally equal — still a new shallow clone for
 * safety).
 */
export function cascadeDeleteEntry(signal, entryId) {
  if (!signal || typeof signal !== 'object') return signal;
  const rules = signal.rules || emptyRules();
  const entries = Array.isArray(rules.entries) ? rules.entries : [];
  const exits = Array.isArray(rules.exits) ? rules.exits : [];
  const nextEntries = entries.filter((b) => b && b.id !== entryId);
  const nextExits = exits.filter((b) => b && b.target_entry_block_id !== entryId);
  return {
    ...signal,
    rules: { entries: nextEntries, exits: nextExits },
  };
}
