/**
 * One-line summary of a single basket leg's inner instrument. Used to
 * compose an inline-basket label like "Basket: SPY, QQQ".
 */
function basketLegLabel(leg) {
  const inst = leg && leg.instrument;
  if (!inst) return '?';
  if (inst.type === 'spot') return inst.instrument_id || inst.collection || '?';
  if (inst.type === 'continuous') return inst.collection || '?';
  if (inst.type === 'option_stream') {
    const parts = [inst.collection, inst.option_type].filter(Boolean);
    return parts.length > 0 ? parts.join('\u00B7') : '?';
  }
  return '?';
}

/**
 * Format a signal input's instrument for display.
 * Shared between HoldingsList and SignalPickerModal.
 */
export function formatInstrument(inst, fallback = '\u2014') {
  if (!inst) return fallback;
  if (inst.type === 'option_stream') {
    const parts = [inst.collection, inst.option_type];
    if (inst.cycle) parts.push(inst.cycle);
    parts.push(inst.stream);
    if (inst.selection?.kind) parts.push(inst.selection.kind);
    return parts.join(' \u00B7 ');
  }
  if (inst.type === 'continuous') {
    const parts = [inst.collection];
    if (inst.adjustment && inst.adjustment !== 'none') parts.push(inst.adjustment);
    if (inst.cycle) parts.push(inst.cycle);
    return parts.join(' \u00B7 ');
  }
  if (inst.type === 'basket') {
    if (inst.kind === 'saved' && inst.basket_id) {
      return `Basket: ${inst.basket_id}`;
    }
    if (inst.kind === 'inline' && Array.isArray(inst.legs) && inst.legs.length > 0) {
      return `Basket: ${inst.legs.map(basketLegLabel).join(', ')}`;
    }
    return fallback;
  }
  return `${inst.instrument_id || inst.symbol || '?'} (${inst.collection || '?'})`;
}
