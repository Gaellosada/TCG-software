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
//       conditions, [exits only] target_entry_block_name
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
import { MAX_ABS_WEIGHT, SECTIONS } from './storage';

/**
 * Normalise every indicator operand inside a signal spec so that
 * ``params_override`` and ``series_override`` are always present as
 * explicit keys (null if absent). Instrument / constant / null operands
 * pass through unchanged. Non-operand fields (lookback, op, …) are
 * preserved verbatim.
 *
 * Weights are clamped to [-MAX_ABS_WEIGHT, +MAX_ABS_WEIGHT]. Block ids
 * and exit ``target_entry_block_name`` are carried through verbatim.
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

function normaliseInput(input) {
  if (!input || typeof input !== 'object') return input;
  return {
    id: typeof input.id === 'string' ? input.id : '',
    instrument: input.instrument ?? null,
  };
}

function clampWeight(w) {
  const n = typeof w === 'number' ? w : Number(w);
  if (!Number.isFinite(n)) return 0;
  if (n > MAX_ABS_WEIGHT) return MAX_ABS_WEIGHT;
  if (n < -MAX_ABS_WEIGHT) return -MAX_ABS_WEIGHT;
  return n;
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
  if (section === 'exits') {
    // Exit blocks omit block-level input_id entirely (not empty-string)
    // so the backend invariant "exits must not carry input_id" is met.
    // Weight is meaningless on exits and also omitted.
    return {
      id: typeof block.id === 'string' ? block.id : '',
      name: typeof block.name === 'string' ? block.name : '',
      enabled: block.enabled !== false,
      description: typeof block.description === 'string' ? block.description : '',
      conditions,
      target_entry_block_name: typeof block.target_entry_block_name === 'string'
        ? block.target_entry_block_name
        : '',
      requires_reset_block_id: typeof block.requires_reset_block_id === 'string'
        && block.requires_reset_block_id
        ? block.requires_reset_block_id
        : null,
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
    requires_reset_block_id: typeof block.requires_reset_block_id === 'string'
      && block.requires_reset_block_id
      ? block.requires_reset_block_id
      : null,
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
    // Strip null/unfilled entries from seriesMap — the backend's
    // _SeriesRefIn model only accepts {collection, instrument_id} dicts.
    // Also strip the frontend-only ``type`` key so the payload matches
    // the backend schema exactly.
    const cleanMap = {};
    for (const [label, ref] of Object.entries(ind.seriesMap || {})) {
      if (ref && typeof ref === 'object' && ref.collection) {
        const { type: _drop, ...rest } = ref;
        cleanMap[label] = rest;
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
