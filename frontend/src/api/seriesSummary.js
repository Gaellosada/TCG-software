// Tiny helper to fetch a short summary of a series: length + date
// span. Backed by the existing /api/data/{collection}/{instrument}
// endpoint — same path the Data page uses for charts. We cache the
// derived summary in-module so repeated toggles of the "details"
// button don't refetch.
//
// Public shape:
//   getSeriesSummary({ collection, instrument_id }) -> Promise<Summary>
//   Summary = { length, start, end, collection, instrument_id }
//
// Errors propagate — callers render "Could not load preview" using the
// thrown Error's message.

import { getInstrumentPrices } from './data';
import { classifyFetchError, FetchError } from '../utils/fetchError';

const cache = new Map(); // `${collection}:${instrument_id}` -> Promise<Summary>

function key(ref) {
  return `${ref.collection}:${ref.instrument_id}`;
}

// Convert a YYYYMMDD integer (the backend's internal date-int form) OR
// an ISO yyyy-mm-dd string into a normalized ISO-date string. The
// /api/data/{collection}/{instrument} endpoint returns ISO strings, so
// in practice this is a passthrough with a defensive integer branch.
function toIsoDate(value) {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' && Number.isFinite(value)) {
    const n = Math.trunc(value);
    const y = Math.floor(n / 10000);
    const m = Math.floor((n % 10000) / 100);
    const d = n % 100;
    const mm = String(m).padStart(2, '0');
    const dd = String(d).padStart(2, '0');
    return `${y}-${mm}-${dd}`;
  }
  return String(value);
}

export function getSeriesSummary(ref) {
  if (!ref || !ref.collection || !ref.instrument_id) {
    return Promise.reject(new Error('Invalid series reference'));
  }
  const k = key(ref);
  if (cache.has(k)) return cache.get(k);
  const p = getInstrumentPrices(ref.collection, ref.instrument_id)
    .then((res) => {
      const dates = (res && res.dates) || [];
      const length = dates.length;
      const start = length > 0 ? toIsoDate(dates[0]) : null;
      const end = length > 0 ? toIsoDate(dates[length - 1]) : null;
      return {
        collection: ref.collection,
        instrument_id: ref.instrument_id,
        length,
        start,
        end,
      };
    })
    .catch((err) => {
      // Evict failed promise so a retry can reattempt the fetch.
      cache.delete(k);
      // Re-throw as a FetchError so callers get {kind, title, message}.
      // ``data.js`` already throws FetchError; if that propagated, pass it
      // through. Otherwise classify defensively.
      if (err instanceof FetchError) throw err;
      const classified = classifyFetchError(err);
      throw new FetchError({ ...classified, cause: err });
    });
  cache.set(k, p);
  return p;
}

// Exposed for tests.
export function _resetSeriesSummaryCache() {
  cache.clear();
}
