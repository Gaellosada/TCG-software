/**
 * Hydrate the list of indicators the user has access to. Pulls BOTH:
 *   - default indicators from the registry (hydrated with per-session
 *     overrides from the Indicators localStorage);
 *   - user-authored indicators from that same storage.
 *
 * Returns an array of ``{id, name, code, params, seriesMap}`` — the exact
 * shape the backend ``/api/signals/compute`` request needs for each
 * referenced indicator (we ship these wholesale). ``readonly`` flag is
 * preserved so the OperandPicker dropdown can show all of them.
 */
import { loadState as loadIndicatorState } from '../Indicators/storage';
import { parseIndicatorSpec, reconcileParams, reconcileSeriesMap } from '../Indicators/paramParser';
import { DEFAULT_INDICATORS } from '../Indicators/defaultIndicators';

export function hydrateAvailableIndicators() {
  const saved = loadIndicatorState();
  const defaults = DEFAULT_INDICATORS.map((def) => {
    const savedEntry = saved.defaultState?.[def.id] || {};
    const spec = parseIndicatorSpec(def.code);
    return {
      id: def.id,
      name: def.name,
      code: def.code,
      readonly: true,
      ownPanel: !!def.ownPanel,
      params: reconcileParams(savedEntry.params || {}, spec.params),
      seriesMap: reconcileSeriesMap(savedEntry.seriesMap || {}, spec.seriesLabels),
    };
  });
  const userIndicators = (saved.indicators || []).map((ind) => {
    const spec = parseIndicatorSpec(ind.code || '');
    return {
      id: ind.id,
      name: ind.name,
      code: ind.code || '',
      readonly: false,
      ownPanel: !!ind.ownPanel,
      params: reconcileParams(ind.params || {}, spec.params),
      seriesMap: reconcileSeriesMap(ind.seriesMap || {}, spec.seriesLabels),
    };
  });
  return [...defaults, ...userIndicators];
}
