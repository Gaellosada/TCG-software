// Pure helpers for building a signal-compute request body — v4.
//
// Kept separate from ``SignalsPage.jsx`` so unit tests can import them
// without pulling the Plotly/CodeMirror dependency tree into the test env.
//
// v4 wire contract (PLAN.md § Wire contract):
//   body = { spec: Signal, indicators: IndicatorSpec[] }
// where:
//   - Signal = { id, name, inputs: Input[], rules, settings, doc? }
//   - Input = { id, instrument: InputInstrument }
//   - rules = { entries: Block[], exits: Block[] }
//   - Block = {
//       id, name, enabled, description,
//       input_id, weight (signed, [-100, +100]),
//       conditions, [exits only] target_entry_block_names (string[])
//     }
//   - settings = { dont_repeat: boolean }
//   - IndicatorSpec = { id, name, code, params, seriesMap }
//
// The wire shape mirrors the stored shape exactly — no translation
// anywhere. Weights are normalised to the signed percentage domain
// [-100, +100]; values outside that range are clamped defensively here
// so the backend never sees leverage.
//
// Every indicator operand in ``spec`` is normalised so that
// ``params_override`` and ``series_override`` are always present as
// explicit keys — null if absent. The backend relies on the keys being
// there to run its override-merge step with a deterministic shape.
//
// IMPORTANT: ``normaliseBlock`` is a WHITELIST — every block field that
// must reach the backend MUST be explicitly copied here. Adding a new
// field to the Block schema requires a corresponding line in
// ``normaliseBlock`` AND a new round-trip test in ``requestShape.test.js``.

import { collectIndicatorIds } from '../../api/signals';
import { MAX_ABS_WEIGHT, SECTIONS, coerceResetCount } from './storage';

// Re-exported under a test-only name so the cross-module identity test in
// storage.test.js can assert the wire path uses the SAME coercion as storage
// and the UI (one helper, no drift). Not part of the public request API.
export { coerceResetCount as __coerceResetCountForTests };

/**
 * Normalise every indicator operand inside a signal spec so that
 * ``params_override`` and ``series_override`` are always present as
 * explicit keys (null if absent). Instrument / constant / null operands
 * pass through unchanged. Non-operand fields (lookback, op, …) are
 * preserved verbatim.
 *
 * Weights are clamped to [-MAX_ABS_WEIGHT, +MAX_ABS_WEIGHT]. Block ids
 * and exit ``target_entry_block_names`` are carried through verbatim.
 *
 * Returns a NEW object graph — the caller's ``signal`` is not mutated.
 */
export function normaliseSpecForRequest(signal) {
  if (!signal || typeof signal !== 'object') return signal;
  const rules = signal.rules || {};
  const outRules = { entries: [], exits: [], resets: [] };
  for (const section of SECTIONS) {
    const blocks = Array.isArray(rules[section]) ? rules[section] : [];
    outRules[section] = blocks.map((b) => normaliseBlock(b, section));
  }
  const inputs = Array.isArray(signal.inputs) ? signal.inputs.map(normaliseInput) : [];
  return { ...signal, inputs, rules: outRules };
}

/**
 * Normalise a per-input ``position_cap`` for the wire.
 *
 * ``position_cap`` is a net-position clamp ``[low, high]`` (fractions, e.g.
 * ``[0, 1]`` = long-or-flat). Mirrors the backend contract in
 * ``tcg/core/api/signals.py::_parse_position_cap``: a 2-element array of
 * FINITE numbers with ``low <= high``. ``typeof x === 'number'`` already
 * excludes JS booleans (which the backend also rejects). Anything else →
 * ``undefined`` so the caller OMITS the key, keeping a normal input
 * byte-identical to a pre-feature payload (matches how links/params_override
 * are handled — present only when well-formed).
 *
 * @param {*} raw
 * @returns {[number, number]|undefined}
 */
function normalisePositionCap(raw) {
  if (!Array.isArray(raw) || raw.length !== 2) return undefined;
  const [lo, hi] = raw;
  if (typeof lo !== 'number' || typeof hi !== 'number') return undefined;
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return undefined;
  if (lo > hi) return undefined;
  return [lo, hi];
}

function normaliseInput(input) {
  if (!input || typeof input !== 'object') return input;
  // Only emit position_cap when present + well-formed — otherwise a normal
  // input stays exactly {id, instrument} (no stray key on the wire).
  const cap = normalisePositionCap(input.position_cap);
  return {
    id: typeof input.id === 'string' ? input.id : '',
    instrument: input.instrument ?? null,
    ...(cap !== undefined ? { position_cap: cap } : {}),
  };
}

function clampWeight(w) {
  const n = typeof w === 'number' ? w : Number(w);
  if (!Number.isFinite(n)) return 0;
  if (n > MAX_ABS_WEIGHT) return MAX_ABS_WEIGHT;
  if (n < -MAX_ABS_WEIGHT) return -MAX_ABS_WEIGHT;
  return n;
}

/**
 * Normalise a block-level temporal ``links`` map for the wire.
 *
 * ``links`` is a flat map { "<successor_condition_index>": <within_bars_int> }
 * keyed by the SUCCESSOR condition's index within the block. It records the set
 * of gaps that are THEN boundaries between conjunction groups: present ⇒ THEN,
 * absent ⇒ AND, empty/missing ⇒ CNF. PARTIAL maps are VALID (the backend
 * accepts any subset of ``{1..condCount-1}``) — ``(A AND B) THEN (C AND D)`` is
 * ``links={2:W}`` on a 4-condition block. Each key must be an integer in
 * [1, condCount-1] with a finite window ≥ 1; a stray / malformed entry is
 * DROPPED (not fatal). An empty result ⇒ ``undefined`` so the caller OMITS the
 * field (CNF — byte-identical to a pre-feature payload). Defence-in-depth
 * alongside the storage sanitiser.
 *
 * @param {*} raw
 * @param {number} condCount the block's condition count
 * @returns {Object|undefined} THEN-boundary links map, or undefined to omit.
 */
function normaliseLinks(raw, condCount) {
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

/** Normalise a block's ``fire_mode`` for the wire — "pulse" or "sustained"
 *  (default). Emitted on entries + exits, omitted on resets (backend rejects
 *  it there). */
function normaliseFireMode(raw) {
  return raw === 'pulse' ? 'pulse' : 'sustained';
}

function normaliseBlock(block, section) {
  if (!block || typeof block !== 'object') return block;
  const conditions = Array.isArray(block.conditions)
    ? block.conditions.map(normaliseCondition)
    : [];
  if (section === 'resets') {
    // Reset blocks: whitelist {id, name, conditions, enabled, description}.
    // input_id/weight/target_entry_block_name are signal-global concerns
    // — backend rejects payloads carrying them.
    return {
      id: typeof block.id === 'string' ? block.id : '',
      name: typeof block.name === 'string' ? block.name : '',
      enabled: block.enabled !== false,
      description: typeof block.description === 'string' ? block.description : '',
      conditions,
    };
  }
  // Normalise the reset binding once so the wire binding and the wire count
  // agree: a count only rides the wire when a reset is actually bound
  // (orphan-kill — no binding forces the single-fire default of 1).
  const resetBlockId = typeof block.requires_reset_block_id === 'string'
    && block.requires_reset_block_id
    ? block.requires_reset_block_id
    : null;
  const resetCount = resetBlockId ? coerceResetCount(block.requires_reset_count) : 1;
  // Block-level temporal chain. Entry+exit blocks may carry it; resets reject
  // it (HTTP 400) so it is omitted from the reset literal above. Undefined ⇒
  // omit the key (zero-link == CNF, byte-identical to a pre-feature payload).
  // Full-coverage-or-nothing, keyed against the normalised condition count.
  const links = normaliseLinks(block.links, conditions.length);
  if (section === 'exits') {
    // Exit blocks omit block-level input_id entirely (not empty-string)
    // so the backend invariant "exits must not carry input_id" is met.
    // Weight is meaningless on exits and also omitted.
    // v6: emit the plural ``target_entry_block_names`` array (the backend
    // accepts both the array and the legacy singular, but we always send
    // the array). Non-array / blank entries are dropped defensively.
    const targetNames = Array.isArray(block.target_entry_block_names)
      ? block.target_entry_block_names.filter((n) => typeof n === 'string' && n)
      : [];
    return {
      id: typeof block.id === 'string' ? block.id : '',
      name: typeof block.name === 'string' ? block.name : '',
      enabled: block.enabled !== false,
      description: typeof block.description === 'string' ? block.description : '',
      conditions,
      target_entry_block_names: targetNames,
      requires_reset_block_id: resetBlockId,
      requires_reset_count: resetCount,
      // fire_mode rides on entries + exits (default sustained); resets reject
      // it (HTTP 400) so the reset literal above omits it.
      fire_mode: normaliseFireMode(block.fire_mode),
      // Only present when there is a real chain — see normaliseLinks.
      ...(links !== undefined ? { links } : {}),
    };
  }
  return {
    id: typeof block.id === 'string' ? block.id : '',
    name: typeof block.name === 'string' ? block.name : '',
    enabled: block.enabled !== false,
    description: typeof block.description === 'string' ? block.description : '',
    input_id: typeof block.input_id === 'string' ? block.input_id : '',
    weight: clampWeight(block.weight),
    conditions,
    requires_reset_block_id: resetBlockId,
    requires_reset_count: resetCount,
    // fire_mode rides on entries + exits (default sustained); resets omit it.
    fire_mode: normaliseFireMode(block.fire_mode),
    // Only present when there is a real chain — see normaliseLinks.
    ...(links !== undefined ? { links } : {}),
  };
}

function normaliseCondition(condition) {
  if (!condition || typeof condition !== 'object') return condition;
  const out = { ...condition };
  for (const slot of ['lhs', 'rhs', 'operand', 'min', 'max']) {
    if (slot in out) {
      out[slot] = normaliseOperand(out[slot]);
    }
  }
  return out;
}

function normaliseOperand(operand) {
  if (!operand || typeof operand !== 'object') return operand;
  if (operand.kind !== 'indicator') return operand;
  // ALWAYS emit both override keys — null if absent — so the backend
  // sees a deterministic shape. series_override is { label -> input_id }.
  return {
    ...operand,
    params_override: operand.params_override ?? null,
    series_override: operand.series_override ?? null,
  };
}

/**
 * Build the backend request body for a signal.
 *
 * @param {Object} signal               the spec in its localStorage shape
 * @param {Array}  availableIndicators  indicator specs hydrated from the
 *                                      Indicators localStorage
 *                                      (``{id, name, code, params, seriesMap}``)
 * @returns {{body: Object, missing: string[]}}
 *   ``body`` — the literal POST body
 *     ``{spec, indicators: IndicatorSpec[]}``
 *   ``missing`` — indicator_ids that were referenced but absent from the
 *                 available-indicators array; callers should abort the
 *                 request and surface a validation error.
 */
export function buildComputeRequestBody(signal, availableIndicators) {
  const needed = collectIndicatorIds(signal);
  const indicatorList = [];
  const missing = [];
  // Iterate deterministically so snapshot-like assertions stay stable.
  for (const id of needed) {
    const ind = (availableIndicators || []).find((i) => i.id === id);
    if (!ind) {
      missing.push(id);
      continue;
    }
    // Clean seriesMap for the backend:
    // - Strip the frontend-only ``type`` key from filled entries
    // - For null/unfilled entries, send a placeholder ref — the backend
    //   derives ``series_labels`` from the seriesMap *keys* and the
    //   primary label (idx 0) is always bound via ``input_id``, so the
    //   placeholder value is ignored.  Without the key, the backend
    //   sees an empty ``series_labels`` and rejects the request.
    const cleanMap = {};
    for (const [label, ref] of Object.entries(ind.seriesMap || {})) {
      if (ref && typeof ref === 'object' && ref.collection) {
        const { type: _drop, ...rest } = ref;
        cleanMap[label] = rest;
      } else {
        // Placeholder — backend ignores primary-label value.
        cleanMap[label] = { collection: '_', instrument_id: '_' };
      }
    }
    indicatorList.push({
      id: ind.id,
      name: ind.name,
      code: ind.code,
      params: ind.params,
      seriesMap: cleanMap,
      ownPanel: !!ind.ownPanel,
    });
  }
  return {
    body: {
      spec: normaliseSpecForRequest(signal),
      indicators: indicatorList,
    },
    missing,
  };
}
