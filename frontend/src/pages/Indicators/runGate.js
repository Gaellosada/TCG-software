// User-visible Run-disabled tooltip strings are preserved verbatim.
export function areAllSlotsFilled(selectedIndicator, seriesLabels) {
  return !!selectedIndicator
    && seriesLabels.length > 0
    && seriesLabels.every((lbl) => {
      const picked = selectedIndicator.seriesMap?.[lbl];
      if (!picked || !picked.collection) return false;
      // Continuous series are identified by collection alone — no instrument_id.
      if (picked.type === 'continuous') return true;
      // Spot (and legacy entries without a type field) require instrument_id.
      return !!picked.instrument_id;
    });
}

export function computeRunDisabledReason(selectedIndicator, seriesLabels) {
  if (!selectedIndicator) return 'Select an indicator first';
  if (!selectedIndicator.code || !selectedIndicator.code.trim()) return 'Add code before running';
  const emptyLabel = seriesLabels.find((lbl) => {
    const picked = selectedIndicator.seriesMap?.[lbl];
    if (!picked || !picked.collection) return true;
    if (picked.type === 'continuous') return false;
    return !picked.instrument_id;
  });
  if (emptyLabel) return `Fill series slot: ${emptyLabel}`;
  return 'Cannot run';
}
