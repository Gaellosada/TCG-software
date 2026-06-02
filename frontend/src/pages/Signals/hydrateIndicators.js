/**
 * Hydrate the list of indicators the user has access to. Pulls BOTH:
 *   - default indicators from the registry;
 *   - user-authored indicators from the MongoDB backend.
 *
 * Returns an array of ``{id, name, code, params, seriesMap}`` -- the exact
 * shape the backend ``/api/signals/compute`` request needs for each
 * referenced indicator (we ship these wholesale). ``readonly`` flag is
 * preserved so the OperandPicker dropdown can show all of them.
 */
import { parseIndicatorSpec, reconcileParams, reconcileSeriesMap } from '../Indicators/paramParser';
import { DEFAULT_INDICATORS } from '../Indicators/defaultIndicators';
import { listIndicators } from '../../api/persistence';

export async function hydrateAvailableIndicators() {
  const defaults = DEFAULT_INDICATORS.map((def) => {
    const spec = parseIndicatorSpec(def.code);
    return {
      id: def.id,
      name: def.name,
      code: def.code,
      readonly: true,
      ownPanel: !!def.ownPanel,
      params: reconcileParams({}, spec.params),
      seriesMap: reconcileSeriesMap({}, spec.seriesLabels),
    };
  });

  let userIndicators = [];
  try {
    const docs = await listIndicators();
    userIndicators = docs.map((doc) => {
      const def = doc.definition || {};
      const code = def.code || '';
      const spec = parseIndicatorSpec(code);
      return {
        id: doc.id,
        name: doc.name,
        code,
        readonly: false,
        ownPanel: !!def.ownPanel,
        params: reconcileParams(def.params || {}, spec.params),
        seriesMap: reconcileSeriesMap(def.seriesMap || {}, spec.seriesLabels),
      };
    });
  } catch {
    // Backend unavailable -- fall back to defaults only.
  }

  return [...defaults, ...userIndicators];
}
