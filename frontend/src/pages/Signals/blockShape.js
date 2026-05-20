// Pure data helpers for Block-level validation — v5.
//
// Single source of truth for "is this block complete enough to Run?".
// Used by the UI (Run gate, per-block status dot) and the request
// builder. No React imports — unit-testable in isolation.
//
// v5 Block shape:
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
// v5 Operand shapes (unchanged from v3):
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
 * @param {'entries'|'exits'|'resets'} section
 */
export function defaultBlock(section = 'entries') {
  const base = {
    id: newBlockId(),
    name: '',
    conditions: [],
    enabled: true,
    description: '',
  };
  if (section === 'exits') {
    base.target_entry_block_name = '';
    // Optional per-block reset binding; null = no gate.
    base.requires_reset_block_id = null;
  } else if (section === 'resets') {
    // Reset blocks are signal-global: no input_id, no weight, no target,
    // and no requires_reset_block_id (a reset cannot gate itself).
  } else {
    base.input_id = '';
    base.weight = 0;
    base.requires_reset_block_id = null;
  }
  return base;
}

/**
 * True iff an input's instrument is fully configured.
 *   - spot:          requires collection + instrument_id.
 *   - continuous:    requires collection + adjustment + cycle + rollOffset + strategy.
 *   - option_stream: requires collection + option_type + maturity + selection + stream.
 *   - basket:        two shapes (locked descriptor; see InstrumentPickerModal):
 *                    - {kind:'saved',   basket_id}                  → non-empty basket_id.
 *                    - {kind:'inline',  asset_class, legs}          → non-empty legs array;
 *                                                                     each leg's `instrument`
 *                                                                     sub-object is configured
 *                                                                     per its `instrument.type`
 *                                                                     (Spot / Continuous /
 *                                                                     OptionStream — strict per-
 *                                                                     class mapping with
 *                                                                     asset_class enforced by
 *                                                                     the BE).  Each leg also
 *                                                                     needs a finite non-zero
 *                                                                     weight.  asset_class must
 *                                                                     be one of the locked
 *                                                                     literals.
 */
const _BASKET_ASSET_CLASSES = ['future', 'option', 'index', 'equity'];

// Per-asset-class strict mapping (mirrors `_ASSET_CLASS_TO_INSTRUMENT_TYPE`
// in `tcg/core/api/_models.py` — the BE enforces the same map via
// `BasketRefInline._check_strict_per_class_mapping`).
const _BASKET_ASSET_CLASS_TO_INSTRUMENT_TYPE = {
  equity: 'spot',
  index: 'spot',
  future: 'continuous',
  option: 'option_stream',
};

/**
 * True iff a leg's `instrument` sub-object is fully configured for its
 * declared `type` (Spot / Continuous / OptionStream).  Switched on the
 * discriminator so each branch verifies only the fields its server-
 * side counterpart requires.
 */
function isInstrumentRefConfigured(inst) {
  if (!inst || typeof inst !== 'object') return false;
  if (inst.type === 'spot') {
    return typeof inst.collection === 'string' && inst.collection.length > 0
      && typeof inst.instrument_id === 'string' && inst.instrument_id.length > 0;
  }
  if (inst.type === 'continuous') {
    // Adjustment / cycle / rollOffset all have BE-side defaults; we
    // only require `collection` here (matches the BE
    // ContinuousInstrumentRef Pydantic — only `type` and `collection`
    // are required).
    return typeof inst.collection === 'string' && inst.collection.length > 0;
  }
  if (inst.type === 'option_stream') {
    return !!(typeof inst.collection === 'string' && inst.collection.length > 0
      && (inst.option_type === 'C' || inst.option_type === 'P')
      && inst.maturity && typeof inst.maturity === 'object' && typeof inst.maturity.kind === 'string'
      && inst.selection && typeof inst.selection === 'object' && typeof inst.selection.kind === 'string'
      && typeof inst.stream === 'string' && inst.stream.length > 0);
  }
  return false;
}

export function isInputConfigured(input) {
  if (!input || typeof input !== 'object') return false;
  if (!input.id || typeof input.id !== 'string') return false;
  const inst = input.instrument;
  if (!inst || typeof inst !== 'object') return false;
  if (inst.type === 'spot' || inst.type === 'continuous' || inst.type === 'option_stream') {
    // Top-level instrument check — same dispatch as the basket-leg
    // helper (DRY).  The continuous variant at the top level still
    // requires the full adjustment/cycle/rollOffset/strategy spec
    // because the picker emits those fields together.
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
    // option_stream:
    return isInstrumentRefConfigured(inst);
  }
  if (inst.type === 'basket') {
    if (inst.kind === 'saved') {
      return typeof inst.basket_id === 'string' && inst.basket_id.length > 0;
    }
    if (inst.kind === 'inline') {
      if (!_BASKET_ASSET_CLASSES.includes(inst.asset_class)) return false;
      if (!Array.isArray(inst.legs) || inst.legs.length === 0) return false;
      const expectedType = _BASKET_ASSET_CLASS_TO_INSTRUMENT_TYPE[inst.asset_class];
      for (const leg of inst.legs) {
        if (!leg || typeof leg !== 'object') return false;
        // Each leg = `{instrument: <discriminated>, weight}` (iter-3
        // polymorphic shape — mirrors `BasketLeg` on the BE wire).
        if (!isInstrumentRefConfigured(leg.instrument)) return false;
        // Strict per-class mapping — the BE rejects mismatches with
        // 422; the FE refuses to call the basket "configured" until
        // the renderer has emitted the right `instrument.type`.
        if (leg.instrument.type !== expectedType) return false;
        if (!Number.isFinite(leg.weight) || leg.weight === 0) return false;
      }
      return true;
    }
    return false;
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
 * Build the set of entry block ids declared on a signal. Used for
 * entry-side runnability checks in isBlockRunnable.
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
  if (section === 'resets') {
    // Reset blocks need ≥1 complete condition; no input_id, weight,
    // or target_entry_block_name. The condition completeness check
    // above already validates operands resolve against declared inputs.
    return true;
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
    // Resolve by name: find exactly one entry with this name
    const matches = (Array.isArray(entryIdsOrBlocks) ? entryIdsOrBlocks : [])
      .filter((b) => b && b.name === tgt);
    if (matches.length !== 1) return false; // 0 = dangling, >1 = ambiguous
    const targetEntry = matches[0];
    if (typeof targetEntry.input_id !== 'string' || !targetEntry.input_id) return false;
    const boundInput = byId[targetEntry.input_id];
    if (!boundInput) return false;
    if (!isInputConfigured(boundInput)) return false;
  }
  return true;
}
