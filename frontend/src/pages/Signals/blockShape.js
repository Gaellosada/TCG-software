// Pure data helpers for Block-level validation — v4.
//
// Single source of truth for "is this block complete enough to Run?".
// Used by the UI (Run gate, per-block status dot) and the request
// builder. No React imports — unit-testable in isolation.
//
// v4 Block shape:
//   {
//     id: <uuid>,
//     input_id: <string>,
//     weight: <float in [-100, +100]>,  // signed; nonzero for entries
//     conditions: Condition[],
//     // only on exit blocks:
//     target_entry_block_id: <uuid>,
//   }
//
// v4 Operand shapes (unchanged from v3):
//   - indicator:  { kind:'indicator', indicator_id, input_id, output,
//                   params_override, series_override }
//   - instrument: { kind:'instrument', input_id, field }
//   - constant:   { kind:'constant', value }
//
// Runnability additionally requires every referenced input_id to resolve
// against the signal's ``inputs`` list AND every such input's instrument
// to be fully configured. Exit blocks additionally require their
// ``target_entry_block_id`` to resolve against the signal's entry blocks.

import { operandSlots } from './conditionOps';
import { newBlockId, MAX_ABS_WEIGHT } from './storage';

/**
 * Build a brand-new empty block.
 *   - id: fresh uuid.
 *   - input_id: ''  — user must pick.
 *   - weight:    0  — user must set a signed value.
 *   - conditions: []
 *   - target_entry_block_id: '' (exits only)
 *
 * @param {'entries'|'exits'} section
 */
export function defaultBlock(section = 'entries') {
  const base = { id: newBlockId(), input_id: '', weight: 0, conditions: [] };
  if (section === 'exits') base.target_entry_block_id = '';
  return base;
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
 * Build the set of entry block ids declared on a signal. Used for exit-
 * block target resolution.
 */
export function collectEntryIds(entryBlocks) {
  const out = new Set();
  if (!Array.isArray(entryBlocks)) return out;
  for (const b of entryBlocks) {
    if (b && typeof b.id === 'string' && b.id) out.add(b.id);
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
 *   - entry blocks additionally require a signed weight with
 *     |weight| in (0, MAX_ABS_WEIGHT] (nonzero; no leverage);
 *   - exit blocks additionally require a target_entry_block_id that
 *     resolves against ``entryIds``.
 *
 * @param {Object} block
 * @param {'entries'|'exits'} section
 * @param {Array<Input>} inputs  the signal's declared inputs.
 * @param {Set<string>} [entryIds] ids of entry blocks on the same signal.
 *        Required when section === 'exits'; ignored otherwise.
 */
export function isBlockRunnable(block, section, inputs, entryIds) {
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
  if (section === 'entries') {
    if (!Number.isFinite(block.weight)) return false;
    if (block.weight === 0) return false;
    if (Math.abs(block.weight) > MAX_ABS_WEIGHT) return false;
  } else if (section === 'exits') {
    const ids = entryIds instanceof Set ? entryIds : new Set(entryIds || []);
    const tgt = block.target_entry_block_id;
    if (typeof tgt !== 'string' || !tgt) return false;
    if (!ids.has(tgt)) return false;
  }
  return true;
}
