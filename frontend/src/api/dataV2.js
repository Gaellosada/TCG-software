import { fetchApi } from './client';
import { classifyFetchError, FetchError } from '../utils/fetchError';

// ---------------------------------------------------------------------------
// Database v2 API client — explores the dwh star schema ``tcg_instruments_v2``
// via the ``/api/data-v2`` router. Mirrors the error-classification wrapper
// used in ``api/data.js`` and ``api/options.js`` (each file keeps its own local
// copy so none depends on another). All reads go through the shared
// ``fetchApi`` client, so ``API_BASE`` (``/api``) is prepended automatically.
// ---------------------------------------------------------------------------

async function fetchClassified(path, options = {}) {
  try {
    return await fetchApi(path, options);
  } catch (err) {
    // Let AbortError propagate unwrapped — callers check signal.aborted.
    if (err && err.name === 'AbortError') throw err;
    if (err && err.name === 'ApiError') {
      if (err.errorType === 'network_error') {
        const classified = classifyFetchError(new TypeError(err.message));
        throw new FetchError({ ...classified, cause: err });
      }
      const status = (err.details && err.details.status)
        || (err.errorType === 'not_found' ? 404 : null)
        || (err.errorType === 'validation' ? 400 : null)
        || (err.errorType === 'server_error' ? 500 : null)
        || null;
      if (status) {
        const classified = classifyFetchError(null, { status }, err.message);
        throw new FetchError({ ...classified, cause: err });
      }
    }
    const classified = classifyFetchError(err);
    throw new FetchError({ ...classified, cause: err });
  }
}

/**
 * GET /api/data-v2/objects
 * → [{ object_id, kind, symbol, name, cycle, underlying_object_id }]
 * All kinds (rate / index / future / option); the FE groups by ``kind``.
 */
export async function listObjectsV2({ signal } = {}) {
  const res = await fetchClassified('/data-v2/objects', { signal });
  // The endpoint returns a bare array; tolerate a ``{ objects: [...] }`` wrap.
  return Array.isArray(res) ? res : (res.objects || []);
}

/**
 * GET /api/data-v2/objects/{object_id}
 * → { object, contracts:[{contract_id, contract_code, expiration, strike,
 *     option_type, multiplier}], series:[{serie_id, contract_id, type, freq,
 *     source}] }
 */
export async function getObjectDetailV2(objectId, { signal } = {}) {
  const res = await fetchClassified(
    `/data-v2/objects/${encodeURIComponent(objectId)}`,
    { signal },
  );
  return res;
}

/**
 * GET /api/data-v2/series/{serie_id}?start&end
 * → { serie_id, type, fields:[...], points:{ ts:[...], <field>:[...] } }
 * ``type`` dispatches the chartable field set (bar→OHLCV+OI, value→value,
 * greeks→…, bbba→…).
 */
export async function getSeriesV2(serieId, { start, end, signal } = {}) {
  const params = new URLSearchParams();
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  const query = params.toString() ? `?${params}` : '';
  const res = await fetchClassified(
    `/data-v2/series/${encodeURIComponent(serieId)}${query}`,
    { signal },
  );
  return res;
}

/**
 * GET /api/data-v2/continuous/futures/{object_id}
 *     ?strategy&adjustment&cycle&roll_offset&rank&start&end
 * → v1-continuous shape family: { dates, open, high, low, close, volume,
 *   roll_dates, contracts } (reviewer: confirm BE returns v1-shaped price
 *   arrays keyed ``dates``/``close`` etc., not a nested ``prices`` object).
 * Mirrors ``getContinuousSeries`` in api/data.js (same param encoding).
 */
export async function getContinuousFuturesV2(objectId, {
  strategy = 'front_month',
  adjustment = 'none',
  cycle,
  rollOffset,
  rank,
  start,
  end,
} = {}) {
  const params = new URLSearchParams();
  params.set('strategy', strategy);
  params.set('adjustment', adjustment);
  if (cycle) params.set('cycle', cycle);
  if (rollOffset > 0) params.set('roll_offset', String(rollOffset));
  if (rank > 1) params.set('rank', String(rank));
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  const res = await fetchClassified(
    `/data-v2/continuous/futures/${encodeURIComponent(objectId)}?${params}`,
  );
  return res;
}

/** GET /api/data-v2/continuous/futures/{object_id}/cycles → available cycles. */
export async function getV2FuturesCycles(objectId) {
  const res = await fetchClassified(
    `/data-v2/continuous/futures/${encodeURIComponent(objectId)}/cycles`,
  );
  return Array.isArray(res) ? res : (res.cycles || []);
}

/**
 * GET /api/data-v2/continuous/options/{object_id}
 *     ?criterion=strike|moneyness&target&option_type=call|put&roll=at_expiry
 *     &start&end
 * → { points:{ ts, value }, roll_dates, contracts, spot_source? }
 * ``criterion=delta`` is rejected by the BE (422/400 "greeks unavailable in
 * v2"); the FE also greys the Delta option so this path is unreachable from UI.
 */
export async function getContinuousOptionsV2(objectId, {
  criterion = 'strike',
  target,
  optionType = 'put',
  roll = 'at_expiry',
  start,
  end,
} = {}) {
  const params = new URLSearchParams();
  params.set('criterion', criterion);
  if (target !== undefined && target !== null && target !== '') {
    params.set('target', String(target));
  }
  params.set('option_type', optionType);
  params.set('roll', roll);
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  const res = await fetchClassified(
    `/data-v2/continuous/options/${encodeURIComponent(objectId)}?${params}`,
  );
  return res;
}
