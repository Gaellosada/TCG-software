// Pure data helpers for Block-level validation — v3 (iter-4).
//
// Single source of truth for "is this block complete enough to Run?".
// Used by the UI (Run gate, per-block status dot) and the request
// builder. No React imports — unit-testable in isolation.
//
// v3 Block shape:
//   { input_id: string, weight: number, conditions: Condition[] }
// v3 Operand shapes:
//   - indicator:  { kind:'indicator', indicator_id, input_id, output,
//                   params_override, series_override }
//   - instrument: { kind:'instrument', input_id, field }
//   - constant:   { kind:'constant', value }
//
// Runnability additionally requires every referenced input_id to resolve
// against the signal's ``inputs`` list AND every such input's instrument
// to be fully configured.

import { operandSlots } from './conditionOps';

/**
 * Build a brand-new empty block.
 * - input_id: ''  — user must pick.
 * - weight:    0  — no pre-filled contribution.
 * - conditions: []
 */
export function defaultBlock() {
  return { input_id: '', weight: 0, conditions: [] };
}

/**
 * True iff an input's instrument is fully configured.
 *   - spot:       requires collection + instrument_id.
 *   - continuous: requires collection + adjustment + cycle + rollOffset + strategy.
 */
export function isInputConfigured(input) {
  if (!input || typeof input !== 'object') return false;
  if (!input.id || typeof input.id !== 'string') return false;
  const inst = input.instrument;
  if (!inst || typeof inst !== 'object') return false;
  if (inst.type === 'spot') {
    return typeof inst.collection === 'string' && inst.collection.length > 0
      && typeof inst.instrument_id === 'string' && inst.instrument_id.length > 0;
  }
  if (inst.type === 'continuous') {
    return typeof inst.collection === 'string' && inst.collection.length > 0
      && ['none', 'proportional', 'difference'].includes(inst.adjustment)
      && (inst.cycle == null || typeof inst.cycle === 'string')
      && Number.isFinite(inst.rollOffset)
      && inst.strategy === 'front_month';
  }
  return false;
}

/**
 * True iff the operand is complete AND any referenced input_ids resolve
 * against ``inputsById``.
 */
export function isOperandComplete(operand, inputsById) {
  if (!operand || typeof operand !== 'object') return false;
  if (operand.kind === 'constant') {
    return Number.isFinite(operand.value);
  }
  const byId = inputsById || {};
  if (operand.kind === 'indicator') {
    if (typeof operand.indicator_id !== 'string' || !operand.indicator_id) return false;
    if (typeof operand.input_id !== 'string' || !operand.input_id) return false;
    if (!byId[operand.input_id]) return false;
    // series_override values (label -> input_id) must also resolve.
    const so = operand.series_override;
    if (so && typeof so === 'object') {
      for (const k of Object.keys(so)) {
        if (!byId[so[k]]) return false;
      }
    }
    return true;
  }
  if (operand.kind === 'instrument') {
    if (typeof operand.input_id !== 'string' || !operand.input_id) return false;
    if (!byId[operand.input_id]) return false;
    if (typeof operand.field !== 'string' || !operand.field) return false;
    return true;
  }
  return false;
}

/** True iff every operand slot on a condition is complete. */
export function isConditionComplete(condition, inputsById) {
  if (!condition || typeof condition !== 'object') return false;
  if (typeof condition.op !== 'string' || !condition.op) return false;
  for (const slot of operandSlots(condition.op)) {
    if (!isOperandComplete(condition[slot], inputsById)) return false;
  }
  return true;
}

/**
 * Build an ``input_id -> input`` map from the signal's declared inputs.
 */
export function indexInputs(inputs) {
  const out = {};
  if (!Array.isArray(inputs)) return out;
  for (const i of inputs) {
    if (i && typeof i.id === 'string' && i.id) out[i.id] = i;
  }
  return out;
}

/**
 * True iff the block can be submitted to the backend.
 *   - input_id resolves to a declared Input that is itself fully
 *     configured (isInputConfigured);
 *   - at least one condition;
 *   - every condition complete (every operand complete and every
 *     referenced input_id resolves);
 *   - entry directions additionally require weight > 0.
 *
 * @param {Object} block
 * @param {string} direction — long_entry / long_exit / short_entry / short_exit
 * @param {Array<Input>} inputs — the signal's declared inputs array.
 */
export function isBlockRunnable(block, direction, inputs) {
  if (!block || typeof block !== 'object') return false;
  if (typeof block.input_id !== 'string' || !block.input_id) return false;
  const byId = indexInputs(inputs);
  const bound = byId[block.input_id];
  if (!bound) return false;
  if (!isInputConfigured(bound)) return false;
  if (!Array.isArray(block.conditions) || block.conditions.length === 0) return false;
  for (const c of block.conditions) {
    if (!isConditionComplete(c, byId)) return false;
  }
  if (direction === 'long_entry' || direction === 'short_entry') {
    if (!Number.isFinite(block.weight) || block.weight <= 0) return false;
  }
  return true;
}
