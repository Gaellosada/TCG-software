/**
 * Format a signal input's instrument for display.
 * Shared between HoldingsList and SignalPickerModal.
 */
export function formatInstrument(inst, fallback = '\u2014') {
  if (!inst) return fallback;
  if (inst.type === 'continuous') {
    const parts = [inst.collection];
    if (inst.adjustment && inst.adjustment !== 'none') parts.push(inst.adjustment);
    if (inst.cycle) parts.push(inst.cycle);
    return parts.join(' \u00B7 ');
  }
  return `${inst.instrument_id || inst.symbol || '?'} (${inst.collection || '?'})`;
}
