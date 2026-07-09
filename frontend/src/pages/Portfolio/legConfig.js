/**
 * Translation between a portfolio LEG and the InstrumentPickerModal's own
 * discriminated union (the shape the modal emits via `onSelect` and consumes
 * via `initialConfig`). Portfolio legs are flattened and field-renamed relative
 * to that union, so both directions live here as pure, round-trippable helpers:
 *
 *   modal union  --instrumentToLegConfig-->  leg config  (add + edit forward)
 *   leg          --legToInitialConfig---->  modal union  (edit pre-fill, inverse)
 *
 * Field gotchas (must be exact — an off-by-one here silently rewrites a leg):
 *   - spot:       leg {type:'instrument', symbol}  <->  modal {type:'spot', instrument_id}
 *   - continuous: camelCase `rollOffset` on BOTH sides; carries `adjustment`.
 *   - option:     snake_case `roll_offset` {value,unit}; NO `adjustment` field
 *                 (option streams carry no back-adjustment); a portfolio option
 *                 leg is the PRICE only and is ALWAYS held (fixed-contract
 *                 $-P&L), so the forward direction forces `hold_between_rolls`.
 */

/**
 * Forward: an InstrumentPickerModal `onSelect` union -> the leg CONFIG fields
 * (no `label`/`weight`/`id` — those are identity/derived and owned by the
 * caller). Shared by the add flow (which then stamps a label + default weight)
 * and the edit flow (which merges these fields into the existing leg).
 */
export function instrumentToLegConfig(instrument) {
  if (instrument.type === 'option_stream') {
    return {
      type: 'option_stream',
      collection: instrument.collection,
      option_type: instrument.option_type,
      cycle: instrument.cycle,
      maturity: instrument.maturity,
      selection: instrument.selection,
      // Roll offset from OptionStreamForm — the unified {value, unit} object,
      // forwarded whole. Option streams carry NO back-adjustment (no
      // `adjustment` field, unlike continuous).
      stream: instrument.stream,
      roll_offset: instrument.roll_offset,
      // PORTFOLIO option price legs are ALWAYS held (the backend requires it),
      // so force hold on regardless of form state.
      hold_between_rolls: true,
      nav_times: instrument.nav_times ?? 1.0,
      // SIZING MODE — forward the futures-notional config ONLY when the user
      // opted into it. Premium-notional (the default) adds NO keys, so a leg the
      // user never touches serialises byte-identically to today (backend default
      // is premium_notional / nearest_on_or_after).
      ...(instrument.sizing_mode === 'futures_notional'
        ? {
          sizing_mode: 'futures_notional',
          futures_reference: instrument.futures_reference || 'nearest_on_or_after',
        }
        : {}),
    };
  }
  if (instrument.type === 'continuous') {
    return {
      type: 'continuous',
      collection: instrument.collection,
      strategy: instrument.strategy,
      adjustment: instrument.adjustment,
      cycle: instrument.cycle,
      rollOffset: instrument.rollOffset,
      // NTH_NEAREST rank — carried only when the modal emitted it (i.e. the
      // strategy is nth_nearest). Omitted for front-month / end-of-month so the
      // leg config stays byte-identical to before.
      ...(instrument.strategy === 'nth_nearest' && Number.isInteger(instrument.rank)
        ? { rank: instrument.rank }
        : {}),
    };
  }
  // spot -> instrument leg: rename instrument_id -> symbol, type spot -> instrument.
  return {
    type: 'instrument',
    collection: instrument.collection,
    symbol: instrument.instrument_id,
  };
}

/**
 * Inverse: an existing leg -> the modal `initialConfig` union that pre-fills the
 * picker when editing. Returns `null` for legs with no config step to seed
 * (signal legs; also anything unrecognised). Reads only type-relevant fields so
 * stale cross-type fields on a full-shape leg do not leak into the union.
 */
export function legToInitialConfig(leg) {
  if (!leg) return null;
  if (leg.type === 'option_stream') {
    return {
      type: 'option_stream',
      collection: leg.collection,
      option_type: leg.option_type,
      cycle: leg.cycle,
      maturity: leg.maturity,
      selection: leg.selection,
      stream: leg.stream,
      roll_offset: leg.roll_offset,
      hold_between_rolls: leg.hold_between_rolls,
      nav_times: leg.nav_times,
      // Restore the sizing mode so editing pre-fills the futures-notional control
      // (undefined on a premium-notional leg → the form's default takes over).
      sizing_mode: leg.sizing_mode,
      futures_reference: leg.futures_reference,
    };
  }
  if (leg.type === 'continuous') {
    return {
      type: 'continuous',
      collection: leg.collection,
      adjustment: leg.adjustment,
      cycle: leg.cycle,
      rollOffset: leg.rollOffset,
      strategy: leg.strategy,
      // Restore the NTH_NEAREST rank so editing pre-fills the rank input
      // (undefined on a non-nth_nearest leg → the picker's default takes over).
      rank: leg.rank,
    };
  }
  if (leg.type === 'instrument') {
    // instrument leg -> spot union: rename symbol -> instrument_id.
    return {
      type: 'spot',
      collection: leg.collection,
      instrument_id: leg.symbol,
    };
  }
  // signal / basket / unknown: no terminal config step to seed.
  return null;
}
