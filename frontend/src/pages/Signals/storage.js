// Local persistence for the Signals page state — schema v3 (iter-4).
//
// All direct ``localStorage`` access for signals lives in this module —
// other modules MUST go through load/save here.
//
// Schema v3 (iter-4):
//   {
//     "version": 3,
//     "signals": [
//       {
//         "id", "name",
//         "inputs": [
//           { "id": "X", "instrument": InputInstrument }
//         ],
//         "rules": { long_entry, long_exit, short_entry, short_exit }
//       }
//     ]
//   }
//
// InputInstrument is a discriminated union:
//   - Spot:        { type: 'spot',       collection, instrument_id }
//   - Continuous:  { type: 'continuous', collection, adjustment, cycle,
//                    rollOffset, strategy }
//
// A block is
//   { input_id: string, weight: number, conditions: Condition[] }
// where ``input_id`` is '' on brand-new blocks (user must pick).
//
// Operand shapes (stored verbatim):
//   - indicator:   { kind:'indicator', indicator_id, input_id, output,
//                    params_override, series_override }
//   - instrument:  { kind:'instrument', input_id, field }
//   - constant:    { kind:'constant', value }
//
// Migration policy (iter-4): v2 payloads are DROPPED on load (warn once).
// Inputs were a new architectural feature; no safe migration exists.

import { SIGNALS_STORAGE_KEY } from './storageKeys';

export const SCHEMA_VERSION = 3;

/** Canonical list of direction tab keys. */
export const DIRECTIONS = Object.freeze([
  'long_entry',
  'long_exit',
  'short_entry',
  'short_exit',
]);

/** Produce an empty rules map with all four direction keys present. */
export function emptyRules() {
  const out = {};
  for (const d of DIRECTIONS) out[d] = [];
  return out;
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
  const strategy = raw.strategy === 'front_month' ? 'front_month' : 'front_month';
  return { type: 'continuous', collection, adjustment, cycle, rollOffset, strategy };
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

function sanitiseWeight(raw) {
  const n = typeof raw === 'number' ? raw : Number(raw);
  if (!Number.isFinite(n) || n < 0) return 0;
  return n;
}

function sanitiseBlock(raw) {
  const input_id = typeof raw.input_id === 'string' ? raw.input_id : '';
  const weight = sanitiseWeight(raw.weight);
  const name = typeof raw.name === 'string' ? raw.name : '';
  const conditions = Array.isArray(raw.conditions)
    ? raw.conditions.filter((c) => c && typeof c === 'object' && typeof c.op === 'string')
    : [];
  return { input_id, weight, name, conditions };
}

function sanitiseSignal(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (typeof raw.id !== 'string' || !raw.id) return null;
  const name = typeof raw.name === 'string' && raw.name ? raw.name : 'Untitled';
  const rawInputs = Array.isArray(raw.inputs) ? raw.inputs : [];
  const inputs = rawInputs.map(sanitiseInput).filter((x) => x !== null);
  const rules = emptyRules();
  const rawRules = (raw.rules && typeof raw.rules === 'object') ? raw.rules : {};
  for (const dir of DIRECTIONS) {
    const blocks = Array.isArray(rawRules[dir]) ? rawRules[dir] : [];
    rules[dir] = blocks
      .filter((b) => b && typeof b === 'object')
      .map(sanitiseBlock);
  }
  const doc = typeof raw.doc === 'string' ? raw.doc : '';
  return { id: raw.id, name, inputs, rules, doc };
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
