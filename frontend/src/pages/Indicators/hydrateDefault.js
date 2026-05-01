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
  // compatibleAssetTypes flows through registry → hydrated indicator
  // verbatim. Required by ``runGate.computeAssetCompatibility`` and the
  // picker grey-out logic. Defaults to undefined when the registry
  // entry omits it (back-compat — runGate then treats the indicator as
  // universally compatible).
  if (Array.isArray(def.compatibleAssetTypes)) {
    hydrated.compatibleAssetTypes = def.compatibleAssetTypes.slice();
  }
  // defaultSeries — registry-only metadata describing the per-label
  // SeriesRef the indicator wants pre-bound when the user has not yet
  // picked. Used by ``applyDefaultSeries`` (Wave 2c). Carried verbatim
  // (no per-label cloning — ``applyDefaultSeries`` builds a fresh
  // seriesMap from these and the index-resolver fallback).
  if (def.defaultSeries && typeof def.defaultSeries === 'object') {
    hydrated.defaultSeries = def.defaultSeries;
  }
  return hydrated;
}

// Auto-populate a default's empty seriesMap slots once the resolver
// returns. Only fills slots still pinned to ``null`` (the user may
// already have picked something).
//
// Per-label routing (Wave 2c — metadata-driven, no id-branches):
//   1. If the indicator declares its own ``defaultSeries[label]`` in
//      the registry, use that verbatim. This is the only way option-
//      native indicators (compatibleAssetTypes: ['option']) get a
//      sensible default — the index resolver returns INDEX instruments
//      which would not match their accepted asset class.
//   2. Otherwise fall back to the ambient resolved-index instrument
//      (legacy ``{collection, instrument_id}`` shape from
//      ``resolveDefaultIndexInstrument``) — produces a SpotInstrumentRef.
//   3. If neither is available for a label, leave it as ``null`` (the
//      slot stays empty; the run-gate surfaces the missing pick).
//
// Sign 7 binding: routing is keyed off the indicator's own
// ``defaultSeries`` metadata, NOT off ``id`` literals.
export function applyDefaultSeries(ind, indexDefault) {
  const perLabelDefaults = (ind.defaultSeries && typeof ind.defaultSeries === 'object')
    ? ind.defaultSeries
    : null;
  const hasIndexDefault = !!(indexDefault && indexDefault.collection && indexDefault.instrument_id);
  // Nothing to fill from — bail early.
  if (!perLabelDefaults && !hasIndexDefault) return ind;
  const updated = { ...ind.seriesMap };
  let touched = false;
  for (const [label, picked] of Object.entries(updated)) {
    if (picked !== null) continue;
    if (perLabelDefaults && perLabelDefaults[label]) {
      updated[label] = perLabelDefaults[label];
      touched = true;
    } else if (hasIndexDefault) {
      updated[label] = {
        type: 'spot',
        collection: indexDefault.collection,
        instrument_id: indexDefault.instrument_id,
      };
      touched = true;
    }
  }
  if (!touched) return ind;
  return { ...ind, seriesMap: updated };
}
