// Pure data helpers for Block-level validation — v4.
//
// Single source of truth for "is this block complete enough to Run?".
// Used by the UI (Run gate, per-block status dot) and the request
// builder. No React imports — unit-testable in isolation.
//
// v4 Block shape:
//   entry: {
//     id: <uuid>,
//     input_id: <string>,
//     weight: <float in [-100, +100]>,  // signed; nonzero
//     conditions: Condition[],
//   }
//   exit:  {
//     id: <uuid>,
//     weight: <ignored>,
//     conditions: Condition[],
//     target_entry_block_name: <string>,  // matches an entry's editable name
//   }
// Exit blocks do NOT carry a block-level input_id — the operating
// input is derived from the target entry's input_id. The backend
// rejects exit payloads containing input_id with HTTP 400.
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
// ``target_entry_block_name`` to resolve against the signal's entry blocks.

import { operandSlots } from './conditionOps';
import { newBlockId, MAX_ABS_WEIGHT } from './storage';

/**
 * Build a brand-new empty block.
 *   - id: fresh uuid.
 *   - conditions: []
 *   - entries: input_id: '' (user must pick), weight: 0 (user must set signed value).
 *   - exits:   target_entry_block_name: '' (user must pick);
 *              NO input_id (derived from target entry).
 *
 * @param {'entries'|'exits'} section
 */
export function defaultBlock(section = 'entries') {
  const base = { id: newBlockId(), conditions: [] };
  if (section === 'exits') {
    base.target_entry_block_name = '';
  } else {
    base.input_id = '';
    base.weight = 0;
  }
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
      && ['none', 'ratio', 'difference'].includes(inst.adjustment)
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
 * Build a map of entry name -> entry block for name-based lookups.
 * Only non-empty names are included. Duplicate names map to null (ambiguous).
 */
export function indexEntryNames(entryBlocks) {
  const out = {};
  if (!Array.isArray(entryBlocks)) return out;
  for (const b of entryBlocks) {
    if (b && typeof b.name === 'string' && b.name) {
      if (b.name in out) {
        out[b.name] = null; // duplicate — ambiguous
      } else {
        out[b.name] = b;
      }
    }
  }
  return out;
}

/**
 * True iff the block can be submitted to the backend.
 *   - entries: ``input_id`` resolves to a declared Input that is fully
 *     configured (isInputConfigured); signed weight with |weight| in
 *     (0, MAX_ABS_WEIGHT].
 *   - exits: ``target_entry_block_name`` resolves against ``entryBlocks``
 *     AND the resolved target entry itself has an ``input_id`` set (the
 *     exit inherits it).
 *   - both: at least one condition; every condition complete (every
 *     operand complete and every referenced input_id resolves).
 *
 * @param {Object} block
 * @param {'entries'|'exits'} section
 * @param {Array<Input>} inputs  the signal's declared inputs.
 * @param {Set<string>|Array<Object>} [entryIdsOrBlocks]
 *   For legacy callers: a Set or Array of entry ids (strings) — the
 *   target id resolution still works but we can't check whether the
 *   target's input is set. For the richer check, pass an Array of entry
 *   Block objects. Required when section === 'exits'; ignored otherwise.
 */
export function isBlockRunnable(block, section, inputs, entryIdsOrBlocks) {
  if (!block || typeof block !== 'object') return false;
  const byId = indexInputs(inputs);
  if (!Array.isArray(block.conditions) || block.conditions.length === 0) return false;
  for (const c of block.conditions) {
    if (!isConditionComplete(c, byId)) return false;
  }
  if (section === 'entries') {
    if (typeof block.input_id !== 'string' || !block.input_id) return false;
    const bound = byId[block.input_id];
    if (!bound) return false;
    if (!isInputConfigured(bound)) return false;
    if (!Number.isFinite(block.weight)) return false;
    if (block.weight === 0) return false;
    if (Math.abs(block.weight) > MAX_ABS_WEIGHT) return false;
  } else if (section === 'exits') {
    const tgt = block.target_entry_block_name;
    if (typeof tgt !== 'string' || !tgt) return false;
    // Resolve by name: need entry blocks array
    let targetEntry = null;
    if (Array.isArray(entryIdsOrBlocks)
        && entryIdsOrBlocks.length > 0
        && typeof entryIdsOrBlocks[0] === 'object') {
      // Name-based resolution: find exactly one entry with this name
      const matches = entryIdsOrBlocks.filter((b) => b && b.name === tgt);
      if (matches.length !== 1) return false; // 0 = dangling, >1 = ambiguous
      targetEntry = matches[0];
    } else {
      // Legacy id-based fallback (shouldn't happen in v4, but defensive)
      const ids = entryIdsOrBlocks instanceof Set
        ? entryIdsOrBlocks
        : new Set(entryIdsOrBlocks || []);
      if (!ids.has(tgt)) return false;
    }
    if (targetEntry) {
      if (typeof targetEntry.input_id !== 'string' || !targetEntry.input_id) return false;
      const boundInput = byId[targetEntry.input_id];
      if (!boundInput) return false;
      if (!isInputConfigured(boundInput)) return false;
    }
  }
  return true;
}
