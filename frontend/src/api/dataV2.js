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
 * → { object: { object_id, kind, symbol, name, cycle, underlying_object_id } }
 * Metadata only. Contracts and series come from `getObjectFacetsV2`
 * (aggregated dimensions) and `getObjectSeriesV2` (filtered + paginated) —
 * shipping them here was a 38 MB / ~36 s response.
 *
 * No component calls this today: the browser list already carries every field
 * the detail header renders, so `ObjectDetail` reads `object` from there rather
 * than re-fetching it. Kept because this module mirrors the v2 HTTP surface
 * one-for-one and the endpoint is live and cheap; see `useObjectDetailV2`.
 */
export async function getObjectDetailV2(objectId, { signal } = {}) {
  const res = await fetchClassified(
    `/data-v2/objects/${encodeURIComponent(objectId)}`,
    { signal },
  );
  return res;
}

/**
 * GET /api/data-v2/objects/{object_id}/facets
 * → { object_id, kind, expirations:[{expiration, contracts}], strike_min,
 *     strike_max, option_types:[...], serie_types:[{type, freq, series}],
 *     totals:{contracts, series} }
 * Cheap aggregate over the dimension tables — this is what the filter panel
 * is built from, so it never touches a fact table.
 */
export async function getObjectFacetsV2(objectId, { signal } = {}) {
  return fetchClassified(
    `/data-v2/objects/${encodeURIComponent(objectId)}/facets`,
    { signal },
  );
}

/**
 * Every key ``getObjectSeriesV2`` understands. Anything else is a caller bug —
 * see ``assertKnownSeriesFilters``. ``signal`` belongs here because the hook
 * calls the function as ``{ ...filters, signal }``.
 */
const SERIES_FILTER_KEYS = Object.freeze([
  'expirationMin', 'expirationMax', 'strikeMin', 'strikeMax',
  'optionType', 'serieType', 'freq', 'skip', 'limit', 'signal',
]);

/**
 * Reject unrecognised filter keys BEFORE any request is issued.
 *
 * Without this, an unknown key is silently dropped by the destructuring below
 * and the request goes out with the filter simply not applied — the caller gets
 * a plausible-looking page of the wrong rows and nothing anywhere says so. The
 * three ways that actually happens:
 *
 *   1. snake_case leaking in from the wire vocabulary:
 *      ``{ option_type: 'call' }`` → dropped; the chain comes back unfiltered.
 *   2. a plain typo: ``{ strikeMim: 6000 }`` → dropped.
 *   3. filters and options mis-slotted on the 3-arg hook:
 *      ``useObjectSeriesV2(12, { limit: 50, enabled: false })`` → passes the
 *      ``filters != null`` gate and FETCHES, while the author believes the hook
 *      is disabled. Easy slip, because ``useSeriesV2(serieId, { start, end,
 *      ...options })`` right next door folds params and options into one bag.
 *
 * A throw rather than a ``console.warn`` (the ``api/base.js`` precedent for
 * loud-but-non-fatal) because there is no valid reading of an unknown key: the
 * alternative to failing is returning confidently wrong data. It throws
 * synchronously, before ``fetchClassified``, so no request is issued — and it
 * is a plain ``TypeError``, not a ``FetchError``, because nothing was fetched.
 */
function assertKnownSeriesFilters(filters) {
  const unknown = Object.keys(filters).filter((k) => !SERIES_FILTER_KEYS.includes(k));
  if (unknown.length > 0) {
    throw new TypeError(
      `getObjectSeriesV2: unknown filter key(s) ${unknown.map((k) => `'${k}'`).join(', ')}. `
      + `Filters are camelCase and limited to: ${SERIES_FILTER_KEYS.join(', ')}. `
      + 'snake_case names (option_type, serie_type, strike_min, …) are the WIRE '
      + 'format produced by this function, never its argument format. If you meant '
      + 'to pass query options, they are the THIRD argument of useObjectSeriesV2.',
    );
  }
}

/**
 * Encode a strike bound, or return null when it is genuinely unset.
 *
 * ``''``/null/undefined mean "no bound" (the filter panel clears an input to
 * ''), but a non-finite number is a caller bug that MUST NOT reach the wire:
 * the backend declares ``strike_min: float | None = Query(None)``, and FastAPI
 * accepts the literal string ``"NaN"`` as ``nan`` with HTTP 200 (verified —
 * unlike ``"abc"``/``""``, which it rejects). So ``Number('1e')`` → NaN →
 * ``?strike_min=NaN`` → 200 with ``total: 0``, and the panel reports "no
 * series" for an object that has hundreds, with no error anywhere.
 */
function strikeBound(name, value) {
  if (value === undefined || value === null || value === '') return null;
  const num = Number(value);
  if (!Number.isFinite(num)) {
    // NOT JSON.stringify: it renders NaN and Infinity as "null", which is the
    // single most misleading thing this message could say.
    const shown = typeof value === 'string' ? `'${value}'` : String(value);
    throw new TypeError(
      `getObjectSeriesV2: ${name} must be a finite number, received ${shown}. `
      + 'A NaN bound is accepted by the backend with HTTP 200 and silently '
      + 'matches nothing, so it is rejected here instead.',
    );
  }
  // Emit the parsed number, not the raw input: validating `Number(value)` and
  // then sending `String(value)` would let '  6000  ' through with its spaces.
  // Byte-identical to the raw form for every well-formed value (6000 → '6000',
  // 0 → '0', '6000' → '6000').
  return String(num);
}

/**
 * GET /api/data-v2/objects/{object_id}/series?<filters>&skip&limit
 * → { items:[{serie_id, contract_id, type, freq, source, contract_code,
 *     expiration, strike, option_type}], total, skip, limit }
 * Contract metadata arrives joined, so no contract_id → contract map is needed
 * client-side. An empty `items` with `total: 0` is a normal answer.
 *
 * Domain reminder (v2, NOT v1): ``optionType`` is call|put|both, ``serieType``
 * is bar|value|greeks|bbba|any, ``freq`` is 1m|daily|any. ``limit`` defaults to
 * 50 server-side and is capped at 500 — out of range comes back as HTTP 400.
 *
 * Omission is meaningful: a filter left unset is NOT sent, so the backend
 * applies its own default (option_type=both / serie_type=any / freq=any)
 * rather than receiving an empty string.
 *
 * Throws ``TypeError`` (before issuing anything) on an unknown filter key or a
 * non-finite strike bound — both are silently-wrong-results bugs otherwise.
 */
export async function getObjectSeriesV2(objectId, filters = {}) {
  assertKnownSeriesFilters(filters);
  const {
    expirationMin,
    expirationMax,
    strikeMin,
    strikeMax,
    optionType,
    serieType,
    freq,
    skip,
    limit,
    signal,
  } = filters;
  const params = new URLSearchParams();
  if (expirationMin) params.set('expiration_min', expirationMin);
  if (expirationMax) params.set('expiration_max', expirationMax);
  const strikeMinParam = strikeBound('strikeMin', strikeMin);
  if (strikeMinParam !== null) params.set('strike_min', strikeMinParam);
  const strikeMaxParam = strikeBound('strikeMax', strikeMax);
  if (strikeMaxParam !== null) params.set('strike_max', strikeMaxParam);
  if (optionType) params.set('option_type', optionType);
  if (serieType) params.set('serie_type', serieType);
  if (freq) params.set('freq', freq);
  if (skip) params.set('skip', String(skip));
  if (limit) params.set('limit', String(limit));
  const query = params.toString() ? `?${params}` : '';
  return fetchClassified(
    `/data-v2/objects/${encodeURIComponent(objectId)}/series${query}`,
    { signal },
  );
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
