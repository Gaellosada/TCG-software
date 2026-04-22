// Persistable-payload helpers for the Indicators page. Pure — no
// side-effects. Extracted from IndicatorsPage.jsx.

// Build the storage-shaped payload (same shape the old persistence
// effect wrote).
export function buildPersistablePayload(indicators) {
  const userIndicators = indicators
    .filter((ind) => !ind.readonly)
    .map((ind) => ({
      id: ind.id,
      name: ind.name,
      code: ind.code,
      doc: typeof ind.doc === 'string' ? ind.doc : '',
      params: ind.params,
      seriesMap: ind.seriesMap,
      // ``ownPanel`` is persisted for customs only — defaults source it
      // from the registry (see ``hydrateDefault``), so we intentionally
      // do NOT include it in ``defaultState`` below.
      ownPanel: !!ind.ownPanel,
    }));
  const defaultState = {};
  for (const ind of indicators) {
    if (!ind.readonly) continue;
    defaultState[ind.id] = { params: ind.params, seriesMap: ind.seriesMap };
  }
  return { indicators: userIndicators, defaultState };
}

// Stable-ish serialization for dirty comparison. JSON.stringify of a
// plain object built from sorted entries is stable across re-renders
// so long as the underlying data is the same.
export function serializePersistablePayload(indicators) {
  return JSON.stringify(buildPersistablePayload(indicators));
}
