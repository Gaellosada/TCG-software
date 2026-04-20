// Local persistence for the Signals page state.
//
// All direct ``localStorage`` access for signals lives in this module —
// other modules MUST go through load/save here. This mirrors the
// pattern used by the Indicators page (see ``pages/Indicators/storage.js``)
// but with a completely separate storage key (``tcg.signals.v2``) so the
// two pages never collide.
//
// Schema v2 (iter-3):
//   {
//     "version": 2,
//     "signals": [
//       { "id", "name", "rules": {long_entry, long_exit, short_entry, short_exit} }
//     ]
//   }
//
// A signal's ``rules`` map always contains all four direction keys, each
// an array of blocks. A block is
//   { instrument: InstrumentRef | null, weight: number, conditions: Condition[] }
// where ``instrument`` is ``{collection, instrument_id}`` or null and
// ``weight`` is a finite non-negative number (0 on brand-new blocks).
//
// Condition shape is dictated by the backend contract and stored verbatim
// — the storage layer does NOT validate individual condition variants so
// that future new operators don't require a schema bump.
//
// Migration policy (iter-3): v1 payloads are DROPPED on load. There was
// exactly one demo signal in the v1 wild and the old shape cannot
// represent a per-block instrument / weight. See storageKeys.js.

import { SIGNALS_STORAGE_KEY } from './storageKeys';

export const SCHEMA_VERSION = 2;

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

function getStorage() {
  try {
    if (typeof globalThis !== 'undefined' && globalThis.localStorage) {
      return globalThis.localStorage;
    }
  } catch {
    // Accessing localStorage can throw in some sandboxes.
  }
  return null;
}

// Module-level flag guaranteeing the "incompatible stored state" warning
// fires at most once per page load, regardless of how many times
// ``loadState`` is invoked. Reset only on full reload (module re-evaluation)
// — which matches the UX intent: the user has already seen the warning
// and the empty state, so we don't spam their console.
let incompatibleVersionWarned = false;

// TESTING HOOK — vitest callers flip this back to ``false`` between tests
// so each case can independently assert the one-shot behaviour without
// leaking warn state across the file. Not part of the public API.
export function __resetIncompatibleVersionWarnedForTests() {
  incompatibleVersionWarned = false;
}

/**
 * Sanitise a single loaded signal object.
 *
 * - Missing direction arrays get restored to [].
 * - Non-array or non-object blocks are dropped.
 * - Each block is normalised to ``{instrument, weight, conditions}``:
 *   * ``instrument`` must be ``{collection: string, instrument_id: string}``
 *     otherwise ⇒ null.
 *   * ``weight`` is coerced to a finite non-negative number; anything
 *     else (NaN, strings, negatives) defaults to 0.
 *   * ``conditions`` kept verbatim modulo the {op:string} filter.
 */
function sanitiseSignal(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (typeof raw.id !== 'string' || !raw.id) return null;
  const name = typeof raw.name === 'string' && raw.name ? raw.name : 'Untitled';
  const rules = emptyRules();
  const rawRules = (raw.rules && typeof raw.rules === 'object') ? raw.rules : {};
  for (const dir of DIRECTIONS) {
    const blocks = Array.isArray(rawRules[dir]) ? rawRules[dir] : [];
    rules[dir] = blocks
      .filter((b) => b && typeof b === 'object')
      .map(sanitiseBlock);
  }
  return { id: raw.id, name, rules };
}

function sanitiseInstrument(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const collection = typeof raw.collection === 'string' ? raw.collection : '';
  const instrument_id = typeof raw.instrument_id === 'string' ? raw.instrument_id : '';
  if (!collection || !instrument_id) return null;
  return { collection, instrument_id };
}

function sanitiseWeight(raw) {
  const n = typeof raw === 'number' ? raw : Number(raw);
  if (!Number.isFinite(n) || n < 0) return 0;
  return n;
}

function sanitiseBlock(raw) {
  const instrument = sanitiseInstrument(raw.instrument);
  const weight = sanitiseWeight(raw.weight);
  const conditions = Array.isArray(raw.conditions)
    ? raw.conditions.filter((c) => c && typeof c === 'object' && typeof c.op === 'string')
    : [];
  return { instrument, weight, conditions };
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

/**
 * Persist the Signals page state. ``state`` must have the shape
 * ``{signals: Signal[]}``. Malformed entries are filtered out defensively.
 */
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
    // Quota / access errors — session state keeps working.
  }
}
