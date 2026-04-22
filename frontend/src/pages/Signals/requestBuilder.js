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
//       id, input_id, weight (signed, [-100, +100]),
//       conditions, [exits only] target_entry_block_id
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
 * and exit ``target_entry_block_id`` are carried through verbatim.
 *
 * Returns a NEW object graph — the caller's ``signal`` is not mutated.
 */
export function normaliseSpecForRequest(signal) {
  if (!signal || typeof signal !== 'object') return signal;
  const rules = signal.rules || {};
  const outRules = { entries: [], exits: [] };
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
  const out = {
    id: typeof block.id === 'string' ? block.id : '',
    input_id: typeof block.input_id === 'string' ? block.input_id : '',
    weight: clampWeight(block.weight),
    conditions,
  };
  if (section === 'exits') {
    out.target_entry_block_id = typeof block.target_entry_block_id === 'string'
      ? block.target_entry_block_id
      : '';
  }
  return out;
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
    indicatorList.push({
      id: ind.id,
      name: ind.name,
      code: ind.code,
      params: ind.params,
      seriesMap: ind.seriesMap,
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
