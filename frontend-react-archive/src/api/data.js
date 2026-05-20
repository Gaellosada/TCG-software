import { fetchApi } from './client';
import { classifyFetchError, FetchError } from '../utils/fetchError';

// Thin wrapper around ``fetchApi`` that re-throws network/HTTP failures
// as a ``FetchError`` with a classified ``kind``. The happy-path return
// shape is preserved for every helper below, so callers that don't care
// about the classification are unaffected. Callers that DO care can
// ``catch (e) { if (e.kind === 'offline') ... }``.
async function fetchClassified(path) {
  try {
    return await fetchApi(path);
  } catch (err) {
    // ``fetchApi`` itself throws an ``ApiError`` we constructed in client.js
    // where ``errorType === 'network_error'`` for the fetch-threw case.
    // For HTTP failures we don't have the raw Response anymore — synthesize
    // one so the classifier can branch on the status.
    if (err && err.name === 'ApiError') {
      if (err.errorType === 'network_error') {
        // Navigator may say offline; classifier will pick that up. If not,
        // treat it as a generic network TypeError.
        const classified = classifyFetchError(new TypeError(err.message));
        throw new FetchError({ ...classified, cause: err });
      }
      // Rebuild a synthetic Response with the status pulled from details,
      // if available; otherwise fall back to unknown.
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
    // Anything else — rethrow unclassified as FetchError 'unknown'.
    const classified = classifyFetchError(err);
    throw new FetchError({ ...classified, cause: err });
  }
}

export async function listCollections(assetClass = null) {
  const params = assetClass ? `?asset_class=${assetClass}` : '';
  const res = await fetchClassified(`/data/collections${params}`);
  return res.collections || [];
}

export async function listInstruments(collection, { skip = 0, limit = 50 } = {}) {
  const res = await fetchClassified(`/data/${collection}?skip=${skip}&limit=${limit}`);
  return res; // { items, total, skip, limit }
}

export async function getInstrumentPrices(collection, instrumentId, { start, end, provider } = {}) {
  const params = new URLSearchParams();
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  if (provider) params.set('provider', provider);
  const query = params.toString() ? `?${params}` : '';
  const res = await fetchClassified(`/data/${encodeURIComponent(collection)}/${encodeURIComponent(instrumentId)}${query}`);
  return res; // { dates, open, high, low, close, volume }
}

export async function getContinuousSeries(collection, { strategy = 'front_month', adjustment = 'none', cycle, rollOffset, start, end } = {}) {
  const params = new URLSearchParams();
  params.set('strategy', strategy);
  params.set('adjustment', adjustment);
  if (cycle) params.set('cycle', cycle);
  if (rollOffset > 0) params.set('roll_offset', String(rollOffset));
  if (start) params.set('start', start);
  if (end) params.set('end', end);
  const res = await fetchClassified(`/data/continuous/${encodeURIComponent(collection)}?${params}`);
  return res;
}

export async function getAvailableCycles(collection) {
  const res = await fetchClassified(`/data/continuous/${encodeURIComponent(collection)}/cycles`);
  return res.cycles || [];
}
