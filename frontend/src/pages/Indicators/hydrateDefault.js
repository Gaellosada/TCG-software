import { parseIndicatorSpec, reconcileParams, reconcileSeriesMap } from './paramParser';

// Hydrate a default indicator from the registry + persisted per-session
// state. Returns the merged shape the rest of the page works with.
//
// ``chartMode`` is a registry-only author hint (no user-editable
// counterpart in localStorage) — it flows straight from ``def`` into
// the hydrated object and is NEVER overridden by the ``defaultState``
// overlay, which only carries ``params`` / ``seriesMap``.
export function hydrateDefault(def, savedEntry) {
  const spec = parseIndicatorSpec(def.code);
  const params = reconcileParams(savedEntry?.params || {}, spec.params);
  const seriesMap = reconcileSeriesMap(savedEntry?.seriesMap || {}, spec.seriesLabels);
  const hydrated = {
    id: def.id,
    name: def.name,
    code: def.code,
    doc: typeof def.doc === 'string' ? def.doc : '',
    readonly: true,
    params,
    seriesMap,
    // ownPanel is locked at the registry — users cannot override it for defaults.
    ownPanel: !!def.ownPanel,
  };
  // chartMode is optional — only propagate when the registry entry sets
  // it, so hydrated objects for entries without the hint stay clean
  // (chart falls back to 'lines' via ``IndicatorChart.jsx``).
  if (typeof def.chartMode === 'string' && def.chartMode) {
    hydrated.chartMode = def.chartMode;
  }
  return hydrated;
}

// Auto-populate a default's SPX slot once the resolver returns, but
// only if the slot is still empty (user may already have picked).
export function applyDefaultSeries(ind, defaultSeries) {
  if (!defaultSeries) return ind;
  const updated = { ...ind.seriesMap };
  let touched = false;
  for (const [label, picked] of Object.entries(updated)) {
    if (picked === null) {
      updated[label] = {
        type: 'spot',
        collection: defaultSeries.collection,
        instrument_id: defaultSeries.instrument_id,
      };
      touched = true;
    }
  }
  if (!touched) return ind;
  return { ...ind, seriesMap: updated };
}
