// Local persistence for the Signals page state — schema v8.
//
// All direct ``localStorage`` access for signals lives in this module —
// other modules MUST go through load/save here.
//
// Schema v8:
//   {
//     "version": 8,
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
//   name: <string>,                  // editable display name
//   input_id: <string>,
//   weight: <float in [-100, +100]>, // SIGNED percentage; sign decides long/short
//   conditions: Condition[],         // unchanged from v3
// }
// Exit Block = {
//   id: <uuid>,                      // stable, generated on creation
//   name: <string>,                  // editable display name
//   target_entry_block_names: <string[]>, // each matches an entry's editable
//                                          // `name`; one exit may close many
//                                          // entries (v6). Cross-input allowed.
//   conditions: Condition[],
// }
// Exit blocks do NOT carry block-level input_id or weight; the
// operating input is derived from each target entry's input_id. The
// sanitiser strips any such legacy fields on load.
//
// InputInstrument is a discriminated union:
//   - Spot:          { type: 'spot',          collection, instrument_id }
//   - Continuous:    { type: 'continuous',    collection, adjustment, cycle,
//                      rollOffset, strategy }
//   - OptionStream:  { type: 'option_stream', collection, option_type,
//                      cycle, maturity, selection, stream, roll_offset }
//                      (no adjustment — option streams are raw stitched series)
//
// Operand shapes (stored verbatim):
//   - indicator:   { kind:'indicator', indicator_id, input_id, output,
//                    params_override, series_override }
//   - instrument:  { kind:'instrument', input_id, field }
//   - constant:    { kind:'constant', value }
//
// Migration policy:
//   - v8 (current): the canonical version. Adds block-level ``fire_mode`` on
//     entries + exits (``"pulse"`` = fire once then re-arm; ``"sustained"`` =
//     stay firing while true). It is purely additive with a behaviour-
//     preserving default (a missing value folds to ``"sustained"``, the
//     historical firing behaviour), so a v7 payload is structurally a v8
//     payload minus the field. v8 also relaxes ``links`` from full-coverage-
//     only to arbitrary THEN-boundary subsets — a validation change only, not
//     a shape change (existing full-coverage maps stay valid).
//   - v7: added block-level temporal ``links`` (a flat { successorIdx:
//     withinBars } map; absent ⇒ CNF) and cross ``count``/``window`` scalars
//     (defaults 1/1 ⇒ single-bar crossover). Both additive with behaviour-
//     preserving defaults, so a v6 payload is a v7 payload minus the fields.
//   - v5 → v6: in-place migration of exit blocks. The singular
//     ``target_entry_block_name`` (string) is folded into the plural
//     ``target_entry_block_names`` (string[]): a non-empty name becomes
//     ``[name]``; an empty string becomes ``[]``. The singular key is
//     dropped. No other shape change — v5 and v6 are otherwise identical,
//     so the migration is loss-free.
//   - v6 → v7: a pure version stamp. No shape change — links are absent in
//     all v6 payloads (⇒ CNF), crosses gain their count/window defaults at
//     sanitise time, and retired rolling conditions stay (rendered as a
//     read-only legacy chip; the op dropdown just no longer offers them).
//   - v7 → v8: a pure version stamp. No shape change — fire_mode is absent
//     in all v7 payloads and the sanitiser fills it with "sustained".
//   - any OTHER version: DROPPED on load (single console.warn per page load).
//
// The migrations run on the raw parsed payload BEFORE per-signal
// sanitisation, chained v5→v6→v7→v8, so the sanitiser only ever sees the
// current shape.

import { SIGNALS_STORAGE_KEY } from './storageKeys';

export const SCHEMA_VERSION = 8;

/** Canonical list of rule sections. */
export const SECTIONS = Object.freeze(['entries', 'exits', 'resets']);

/** Max absolute percentage weight — no leverage. */
export const MAX_ABS_WEIGHT = 100;

/** Produce an empty rules map with all sections present. */
export function emptyRules() {
  return { entries: [], exits: [], resets: [] };
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
  const adjustment = ['none', 'ratio', 'difference'].includes(raw.adjustment)
    ? raw.adjustment : 'none';
  const cycle = (typeof raw.cycle === 'string' && raw.cycle) ? raw.cycle : null;
  const rollOffset = Number.isFinite(raw.rollOffset) ? raw.rollOffset : 0;
  // Issue #3: two roll strategies are supported. Validate against the known set
  // (a rogue value still collapses to the default) and PRESERVE end_of_month so
  // a saved signal doesn't silently lose it on load.
  const strategy = ['front_month', 'end_of_month'].includes(raw.strategy)
    ? raw.strategy : 'front_month';
  return { type: 'continuous', collection, adjustment, cycle, rollOffset, strategy };
}

function sanitiseOptionStreamInstrument(raw) {
  const collection = typeof raw.collection === 'string' ? raw.collection : '';
  if (!collection) return null;
  const option_type = ['C', 'P'].includes(raw.option_type) ? raw.option_type : null;
  if (!option_type) return null;
  // maturity and selection are discriminated unions — preserve as-is if they have a valid kind
  const maturity = raw.maturity && typeof raw.maturity === 'object' && typeof raw.maturity.kind === 'string'
    ? raw.maturity : null;
  if (!maturity) return null;
  const selection = raw.selection && typeof raw.selection === 'object' && typeof raw.selection.kind === 'string'
    ? raw.selection : null;
  if (!selection) return null;
  const VALID_STREAMS = ['mid', 'iv', 'delta', 'gamma', 'vega', 'theta', 'open_interest', 'volume'];
  const stream = VALID_STREAMS.includes(raw.stream) ? raw.stream : null;
  if (!stream) return null;
  const cycle = (typeof raw.cycle === 'string' && raw.cycle) ? raw.cycle : null;
  // roll_offset is the unified {value, unit:'days'|'months'} — the ROLL-EARLY
  // axis. Per-unit clamp: days 0..30, months 0..12. A legacy bare int (the old
  // days-only field) reads as {value:int, unit:'days'}. Absent → {0, days}.
  // NOTE: option streams carry NO back-adjustment, so any legacy `adjustment`
  // key is dropped; "roll at end of month" is the EndOfMonth MATURITY, so the
  // former `roll_schedule` field is dropped here too (no pass-through).
  const roll_offset = sanitiseRollOffset(raw.roll_offset);
  return {
    type: 'option_stream', collection, option_type, cycle, maturity, selection,
    stream, roll_offset,
  };
}

function sanitiseRollOffset(raw) {
  const cap = (unit) => (unit === 'months' ? 12 : 30);
  // Legacy bare int → days.
  if (Number.isFinite(raw)) {
    return { value: Math.min(30, Math.max(0, Math.trunc(raw))), unit: 'days' };
  }
  if (raw && typeof raw === 'object') {
    const unit = raw.unit === 'months' ? 'months' : 'days';
    const value = Number.isFinite(raw.value)
      ? Math.min(cap(unit), Math.max(0, Math.trunc(raw.value))) : 0;
    return { value, unit };
  }
  return { value: 0, unit: 'days' };
}

function sanitiseInstrument(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.type === 'option_stream') return sanitiseOptionStreamInstrument(raw);
  if (raw.type === 'continuous') return sanitiseContinuousInstrument(raw);
  // Default/legacy path: treat as spot.
  return sanitiseSpotInstrument(raw);
}

/**
 * Field-local sanitiser for a per-input ``position_cap`` (net-position clamp).
 *
 * Preserves a well-formed ``[low, high]`` pair through the localStorage
 * round-trip; drops anything malformed so a stored value never diverges from
 * what the wire (``requestBuilder.normalisePositionCap``) and the backend
 * (``_parse_position_cap``) accept: a 2-element array of FINITE numbers with
 * ``low <= high``. ``typeof x === 'number'`` already excludes JS booleans.
 * Returns ``undefined`` when absent/malformed so the caller OMITS the key
 * (a normal input stays exactly ``{id, instrument}``).
 *
 * @param {*} raw
 * @returns {[number, number]|undefined}
 */
function sanitisePositionCap(raw) {
  if (!Array.isArray(raw) || raw.length !== 2) return undefined;
  const [lo, hi] = raw;
  if (typeof lo !== 'number' || typeof hi !== 'number') return undefined;
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return undefined;
  if (lo > hi) return undefined;
  return [lo, hi];
}

function sanitiseInput(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const id = typeof raw.id === 'string' ? raw.id : '';
  if (!id) return null;
  const instrument = raw.instrument ? sanitiseInstrument(raw.instrument) : null;
  // Inputs may exist without a fully-configured instrument (user is still
  // picking) — we keep them but signal via instrument=null. The Run-gate
  // checks configuration before submitting.
  const cap = sanitisePositionCap(raw.position_cap);
  return { id, instrument, ...(cap !== undefined ? { position_cap: cap } : {}) };
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

function sanitiseRequiresResetBlockId(raw) {
  // Anything but a non-empty string collapses to null — the sanitiser
  // is field-local; cross-section validity is runGate's job.
  if (typeof raw === 'string' && raw) return raw;
  return null;
}

/**
 * Field-local sanitiser for an exit block's target-entry names (v6).
 *
 * Reads ``raw.target_entry_block_names`` (the canonical plural array) and,
 * for forward-compat, folds in a stray legacy singular
 * ``target_entry_block_name`` if the plural key is absent. Returns a
 * de-duplicated array of non-empty string names (order preserved, first
 * occurrence wins). Empty / malformed input → ``[]``.
 *
 * Cross-section validity (does each name resolve to exactly one entry?) is
 * NOT this function's job — that lives in blockShape/runGate. The sanitiser
 * only guarantees the field is a clean string[] with no blanks or dupes.
 */
function sanitiseTargetEntryNames(raw) {
  let source;
  if (Array.isArray(raw.target_entry_block_names)) {
    source = raw.target_entry_block_names;
  } else if (typeof raw.target_entry_block_name === 'string') {
    // Legacy singular survivor — fold to an array. Empty string → [].
    source = raw.target_entry_block_name ? [raw.target_entry_block_name] : [];
  } else {
    source = [];
  }
  const seen = new Set();
  const out = [];
  for (const n of source) {
    if (typeof n === 'string' && n && !seen.has(n)) {
      seen.add(n);
      out.push(n);
    }
  }
  return out;
}

/**
 * Coerce a raw per-block reset-count to a canonical integer ≥ 1.
 *   - Coerce to a number (numbers pass through; everything else via Number).
 *   - If the result is finite and ≥ 1, floor it; otherwise → 1.
 *   - Anything non-finite or < 1 → 1 (the single-fire default == current
 *     re-arm behavior).
 *
 * THE single source of truth for reset-count coercion. ``requestBuilder.js``
 * (wire) and ``BlockHeader.jsx`` (UI commit/display) import this exact
 * function so all three call sites are byte-identical — guarding against the
 * old ``Number(x)`` vs ``parseFloat(x)`` divergence (e.g. "3px": Number→NaN→1
 * vs parseFloat→3; "": Number→0→1 vs parseFloat→NaN→1).
 *
 * Lives here (not in blockShape.js) to avoid a circular import:
 * ``blockShape.js`` already imports from ``storage.js`` and would form a
 * cycle if storage imported back from it. ``blockShape.js`` and
 * ``requestBuilder.js`` already depend on ``storage.js``, so this adds no new
 * edge. It also sits beside its sibling ``sanitiseWeight`` (same domain).
 *
 * Field-local only; the count is meaningful solely when
 * requires_reset_block_id is set. The sanitiser/normaliser force it to 1 when
 * no reset is bound (see sanitiseBlock / normaliseBlock) so an orphan count
 * can never ride in storage or on the wire. No SCHEMA_VERSION bump:
 * defaulting missing values to 1 gives forward-compat for existing v5
 * signals that predate the field.
 *
 * @param {*} raw
 * @returns {number} integer ≥ 1
 */
export function coerceResetCount(raw) {
  const n = typeof raw === 'number' ? raw : Number(raw);
  if (Number.isFinite(n) && n >= 1) return Math.floor(n);
  return 1;
}

/**
 * Coerce a cross-condition ``count`` or ``window`` scalar to an integer ≥ 1.
 *   - Numbers / numeric strings → floored if finite and ≥ 1.
 *   - Anything non-finite or < 1 → 1 (the single-bar-crossover default,
 *     byte-identical to a pre-feature ``CrossCondition``).
 *
 * Mirrors ``coerceResetCount`` (same int-≥-1 domain) so authoring controls,
 * sanitisation and the wire all agree. ``count``/``window`` default to 1 so
 * every existing cross condition deserialises unchanged.
 *
 * @param {*} raw
 * @returns {number} integer ≥ 1
 */
export function coerceCrossField(raw) {
  const n = typeof raw === 'number' ? raw : Number(raw);
  if (Number.isFinite(n) && n >= 1) return Math.floor(n);
  return 1;
}

/**
 * Field-local sanitiser for a block's temporal ``links`` map.
 *
 * ``links`` is a flat { "<successorIdx>": <withinBars> } map keyed by the
 * SUCCESSOR condition's index within the block. It records the set of gaps that
 * are THEN boundaries between conjunction groups: a gap present ⇒ THEN (a new
 * group starts there), a gap absent ⇒ AND (same group). PARTIAL maps are valid
 * — ``(A AND B) THEN (C AND D)`` is a 4-condition block with ``links={2:W}``.
 * An empty map ⇒ one group ⇒ CNF (byte-identical to a pre-feature payload).
 *
 * The map is cleaned, not rejected wholesale: each key must parse to an integer
 * in [1, condCount-1] with a finite integer window ≥ 1; any stray / malformed
 * entry is DROPPED (not fatal, since a partial map is legitimate). If nothing
 * survives (or < 2 conditions), it returns ``undefined`` ⇒ the field is omitted
 * (CNF).
 *
 * @param {*} raw            the raw links value
 * @param {number} condCount the block's condition count
 * @returns {Object|undefined}
 */
export function sanitiseLinks(raw, condCount) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  if (!Number.isInteger(condCount) || condCount < 2) return undefined;
  const out = {};
  for (const [k, v] of Object.entries(raw)) {
    const idx = Number(k);
    if (!Number.isInteger(idx) || idx < 1 || idx >= condCount) continue;
    const w = typeof v === 'number' ? v : Number(v);
    if (!Number.isFinite(w) || w < 1) continue;
    out[String(idx)] = Math.floor(w);
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * Field-local sanitiser for a block's ``fire_mode`` (entries + exits only).
 *
 * ``"pulse"`` = fire once on the trigger bar then re-arm; ``"sustained"`` = stay
 * active every bar the condition holds. The backend dataclass default and the
 * hydration of stored blocks lacking the field are BOTH ``"sustained"`` (the
 * historical behaviour — this preserves the golden master). New blocks stamp
 * ``"pulse"`` in ``blockShape.defaultBlock``. Anything but the literal
 * ``"pulse"`` collapses to ``"sustained"``.
 */
export function sanitiseFireMode(raw) {
  return raw === 'pulse' ? 'pulse' : 'sustained';
}

/**
 * Sanitise a single stored condition. Pure / field-local.
 *
 * Keeps any condition whose ``op`` is a string (so retired ``rolling_*``
 * conditions survive — they are no longer authorable but stay evaluable and
 * render as a read-only legacy chip). For cross conditions only, coerces the
 * optional ``count``/``window`` scalars to int ≥ 1 (defaulting to 1) so a
 * plain crossover is byte-identical to a pre-feature condition.
 */
function sanitiseCondition(c) {
  if (c.op === 'cross_above' || c.op === 'cross_below') {
    return {
      ...c,
      count: coerceCrossField(c.count),
      window: coerceCrossField(c.window),
    };
  }
  return c;
}

function sanitiseBlock(raw, section) {
  const id = (typeof raw.id === 'string' && raw.id) ? raw.id : newBlockId();
  const name = typeof raw.name === 'string' ? raw.name : '';
  const conditions = Array.isArray(raw.conditions)
    ? raw.conditions
      .filter((c) => c && typeof c === 'object' && typeof c.op === 'string')
      .map(sanitiseCondition)
    : [];
  const enabled = typeof raw.enabled === 'boolean' ? raw.enabled : true;
  const description = typeof raw.description === 'string' ? raw.description : '';
  // Temporal chain: entries+exits only. Resets reject links (the backend
  // 400s), so the reset branch never stores it. ``undefined`` ⇒ omit the
  // field (CNF). Indexed against the sanitised condition count.
  const links = sanitiseLinks(raw.links, conditions.length);
  if (section === 'resets') {
    // Reset blocks are signal-global: no block-level input_id, no weight,
    // no target_entry_block_name, no requires_reset_block_id, no temporal
    // links. Legacy payloads that smuggle these fields have them stripped
    // here so saved state stays canonical.
    return { id, name, conditions, enabled, description };
  }
  if (section === 'exits') {
    // Exit blocks carry no block-level input_id or weight — the
    // operating input is derived from each target entry. Legacy
    // payloads may include these fields; strip them.
    // Legacy target_entry_block_id is also stripped — exits now
    // reference entries by their editable name string.
    const exitReset = sanitiseRequiresResetBlockId(raw.requires_reset_block_id);
    return {
      id,
      name,
      conditions,
      enabled,
      description,
      // v6: plural array of target names. ``sanitiseTargetEntryNames``
      // also folds a stray legacy singular ``target_entry_block_name`` in
      // (belt-and-braces alongside the top-level v5→v6 migration) so a
      // mangled payload can't smuggle the singular key past us.
      target_entry_block_names: sanitiseTargetEntryNames(raw),
      requires_reset_block_id: exitReset,
      // Orphan-kill: a count only means something when a reset is bound.
      // No binding → force the single-fire default so a stale count can't
      // ride in storage.
      requires_reset_count: exitReset ? coerceResetCount(raw.requires_reset_count) : 1,
      // Missing fire_mode folds to "sustained" (the historical behaviour) so
      // stored signals that predate the field are unchanged; new blocks carry
      // "pulse" (stamped by defaultBlock).
      fire_mode: sanitiseFireMode(raw.fire_mode),
      // Only stored when there is a real chain (≥2 conditions + valid map).
      ...(links !== undefined ? { links } : {}),
    };
  }
  const input_id = typeof raw.input_id === 'string' ? raw.input_id : '';
  const weight = sanitiseWeight(raw.weight);
  const entryReset = sanitiseRequiresResetBlockId(raw.requires_reset_block_id);
  return {
    id,
    input_id,
    weight,
    name,
    conditions,
    enabled,
    description,
    requires_reset_block_id: entryReset,
    // Orphan-kill (see exits above).
    requires_reset_count: entryReset ? coerceResetCount(raw.requires_reset_count) : 1,
    // Missing fire_mode folds to "sustained" (historical behaviour); new
    // blocks carry "pulse" (stamped by defaultBlock).
    fire_mode: sanitiseFireMode(raw.fire_mode),
    // Only stored when there is a real chain (≥2 conditions + valid map).
    ...(links !== undefined ? { links } : {}),
  };
}

function sanitiseSettings(_raw) {
  // dont_repeat is always true — the engine is correct and arrows
  // always mark strict position transitions. Legacy signals stored
  // with dont_repeat=false are normalised to true on load.
  return defaultSettings();
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

/**
 * Migrate a parsed v5 payload to v6 in place (returns a new object graph).
 *
 * The ONLY shape change between v5 and v6 is on exit blocks: the singular
 * ``target_entry_block_name`` (string) becomes the plural
 * ``target_entry_block_names`` (string[]). A non-empty name → ``[name]``;
 * an empty / missing name → ``[]``. The singular key is dropped. If a v5
 * exit somehow already carries the plural array it is honoured as-is and
 * the singular key (if any) is dropped.
 *
 * Pure — does not mutate the input. Bumps ``version`` to 6 (the literal — NOT
 * SCHEMA_VERSION — so the migration chain stays explicit: v5→v6 then v6→v7).
 *
 * @param {object} parsed  parsed localStorage payload with ``version === 5``
 * @returns {object}       a v6-shaped payload
 */
export function migrateV5ToV6(parsed) {
  const rawSignals = Array.isArray(parsed.signals) ? parsed.signals : [];
  const signals = rawSignals.map((sig) => {
    if (!sig || typeof sig !== 'object') return sig;
    const rules = (sig.rules && typeof sig.rules === 'object') ? sig.rules : {};
    const exits = Array.isArray(rules.exits) ? rules.exits : [];
    const nextExits = exits.map((ex) => {
      if (!ex || typeof ex !== 'object') return ex;
      // Drop the singular key regardless; derive the plural array from
      // whichever source is present (plural wins if both exist).
      const { target_entry_block_name: legacy, ...rest } = ex;
      let names;
      if (Array.isArray(ex.target_entry_block_names)) {
        names = ex.target_entry_block_names;
      } else if (typeof legacy === 'string' && legacy) {
        names = [legacy];
      } else {
        names = [];
      }
      return { ...rest, target_entry_block_names: names };
    });
    return { ...sig, rules: { ...rules, exits: nextExits } };
  });
  return { ...parsed, version: 6, signals };
}

/**
 * Migrate a parsed v6 payload to v7 in place (returns a new object graph).
 *
 * v7 adds two purely-additive, behaviour-preserving features:
 *   - block-level temporal ``links`` (a flat { successorIdx: withinBars } map;
 *     ABSENT in every v6 payload ⇒ CNF, the default), and
 *   - cross ``count``/``window`` scalars (DEFAULT 1/1 ⇒ today's single-bar
 *     crossover).
 * Neither requires a shape rewrite — a v6 payload IS a v7 payload missing the
 * new optional fields, which ``sanitiseBlock``/``sanitiseCondition`` fill in
 * with their behaviour-preserving defaults. So this migration is a pure
 * version stamp: bump ``version`` to 7 and let the sanitiser do the rest. (We
 * stamp the literal SCHEMA_VERSION = 7; the per-signal sanitisation runs after
 * in ``loadState``.)
 *
 * Pure — does not mutate the input.
 *
 * @param {object} parsed  parsed localStorage payload with ``version === 6``
 * @returns {object}       a v7-shaped payload
 */
export function migrateV6ToV7(parsed) {
  return { ...parsed, version: 7 };
}

/**
 * Migrate a parsed v7 payload to v8 (returns a new object graph).
 *
 * v8 adds one purely-additive, behaviour-preserving field: block-level
 * ``fire_mode`` on entries + exits. Every v7 payload lacks it; ``sanitiseBlock``
 * folds a missing value to ``"sustained"`` (the historical firing behaviour),
 * so a v7 payload IS a v8 payload minus the new field. Pure version stamp —
 * the sanitiser fills the default. (v8 also relaxes ``links`` from
 * full-coverage-only to arbitrary THEN-boundary subsets, but that is a
 * validation change, not a shape change: existing full-coverage maps stay
 * valid, so no data rewrite is needed here either.)
 *
 * Pure — does not mutate the input.
 *
 * @param {object} parsed  parsed localStorage payload with ``version === 7``
 * @returns {object}       a v8-shaped payload
 */
export function migrateV7ToV8(parsed) {
  return { ...parsed, version: SCHEMA_VERSION };
}

/**
 * Produce a duplicate of a signal doc: a deep clone with a fresh signal id,
 * a "(copy)" name suffix, and forced ``locked: false``. Pure.
 *
 * Block ids are DELIBERATELY preserved (not regenerated): they are latch keys
 * scoped to ONE signal's evaluation, so they never collide across signals, and
 * ``requires_reset_block_id`` references them by id — regenerating would break
 * those intra-signal bindings. Category is a persistence concern owned by the
 * caller's create call, not part of this shape.
 *
 * @param {object} doc   source signal (editor shape)
 * @param {{newId?: string, nameSuffix?: string}} [opts]
 * @returns {object|null}
 */
export function duplicateSignal(doc, { newId, nameSuffix = ' (copy)' } = {}) {
  if (!doc || typeof doc !== 'object') return null;
  const clone = JSON.parse(JSON.stringify(doc));
  clone.id = (typeof newId === 'string' && newId) ? newId : newBlockId();
  clone.name = `${doc.name || 'Untitled'}${nameSuffix}`;
  clone.locked = false;
  return clone;
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
  // Migration chain, run BEFORE sanitisation so the sanitiser only ever sees
  // the current shape. v5 → v6: loss-free exit-target singular→plural. v6 → v7:
  // pure version stamp (links/cross defaults are applied by the sanitiser).
  // v7 → v8: pure version stamp for the additive block-level fire_mode field
  // (the sanitiser folds a missing value to "sustained"). Each step is gated on
  // the running version so a v5 payload walks the whole chain (v5→v6→v7→v8).
  // Anything that isn't the current version after the chain is dropped.
  if (parsed.version === 5) {
    parsed = migrateV5ToV6(parsed);
  }
  if (parsed.version === 6) {
    parsed = migrateV6ToV7(parsed);
  }
  if (parsed.version === 7) {
    parsed = migrateV7ToV8(parsed);
  }
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
 * Return a new signal with the entry block ``entryId`` removed, and the
 * deleted entry's name STRIPPED from every exit's
 * ``target_entry_block_names`` array (v6 cascade delete). An exit is
 * removed entirely only if stripping the name leaves its target array
 * EMPTY — exits that still target other surviving entries are kept (with
 * the deleted name pruned). Pure — does not mutate the input.
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
  const deleted = entries.find((b) => b && b.id === entryId);
  const nextEntries = entries.filter((b) => b && b.id !== entryId);
  const deletedName = deleted && typeof deleted.name === 'string' ? deleted.name : '';
  let nextExits = exits;
  if (deletedName) {
    nextExits = [];
    for (const b of exits) {
      if (!b) continue;
      const names = Array.isArray(b.target_entry_block_names)
        ? b.target_entry_block_names
        : [];
      const pruned = names.filter((n) => n !== deletedName);
      // Only drop the exit when removing this target empties its list.
      // An exit that still targets another entry survives, name pruned.
      if (pruned.length === 0) continue;
      nextExits.push(
        pruned.length === names.length
          ? b // nothing changed for this exit — keep the same reference
          : { ...b, target_entry_block_names: pruned },
      );
    }
  }
  // Spread the existing rules so sections like ``resets`` (and any future
  // section listed in ``SECTIONS``) survive the cascade — otherwise the
  // next autosave would silently drop them via sanitiseSignal.
  return {
    ...signal,
    rules: { ...rules, entries: nextEntries, exits: nextExits },
  };
}
