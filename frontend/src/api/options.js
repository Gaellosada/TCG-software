import { fetchApi } from './client';
import { classifyFetchError, FetchError } from '../utils/fetchError';

// Thin wrapper around ``fetchApi`` that re-throws network/HTTP failures
// as a ``FetchError`` with a classified ``kind``. Mirrors the same helper
// defined in ``data.js`` — both files use an identical local copy so neither
// depends on the other.
async function fetchClassified(path) {
  try {
    return await fetchApi(path);
  } catch (err) {
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

// ---------------------------------------------------------------------------
// 1. List option roots
//    GET /api/options/roots
//    Returns: { roots: OptionRootInfo[] }
// ---------------------------------------------------------------------------

export async function getOptionRoots() {
  return fetchClassified('/options/roots');
}

// ---------------------------------------------------------------------------
// 1b. List distinct expirations for a root
//     GET /api/options/expirations?root=OPT_SP_500
//     Returns: { root, expirations: ['YYYY-MM-DD', ...] }
// ---------------------------------------------------------------------------

export async function getOptionExpirations(root) {
  const qp = new URLSearchParams({ root: String(root) });
  return fetchClassified(`/options/expirations?${qp}`);
}

// ---------------------------------------------------------------------------
// 2. Chain query
//    GET /api/options/chain
//    Returns: ChainResponse
//
// params:
//   date          YYYY-MM-DD string (required)
//   type          'C' | 'P' | 'both' (default 'both')
//   expirationMin YYYY-MM-DD string (required)
//   expirationMax YYYY-MM-DD string (required)
//   strikeMin     number (optional)
//   strikeMax     number (optional)
//   computeMissing boolean (optional, default false)
//
// Note: callers must pass date strings in YYYY-MM-DD format. If you have a
// Date object, use: date.toISOString().slice(0, 10)
// ---------------------------------------------------------------------------

export async function getOptionChain(root, params) {
  const {
    date,
    type,
    expirationMin,
    expirationMax,
    strikeMin,
    strikeMax,
    computeMissing,
  } = params;

  const qp = new URLSearchParams();
  qp.set('root', String(root));
  qp.set('date', String(date));
  if (type != null) qp.set('type', String(type));
  qp.set('expiration_min', String(expirationMin));
  qp.set('expiration_max', String(expirationMax));
  if (strikeMin != null) qp.set('strike_min', String(strikeMin));
  if (strikeMax != null) qp.set('strike_max', String(strikeMax));
  if (computeMissing != null) qp.set('compute_missing', String(computeMissing));

  return fetchClassified(`/options/chain?${qp}`);
}

// ---------------------------------------------------------------------------
// 3. Per-contract series
//    GET /api/options/contract/{coll}/{id}
//
// contractId may contain '|' or other special characters (composite IDs such
// as "SPY_240419C00500000|M"). Both path segments are URL-encoded.
//
// options:
//   computeMissing boolean (optional)
//   dateFrom       YYYY-MM-DD string (optional)
//   dateTo         YYYY-MM-DD string (optional)
// ---------------------------------------------------------------------------

export async function getOptionContract(collection, contractId, { computeMissing, dateFrom, dateTo } = {}) {
  const qp = new URLSearchParams();
  if (computeMissing != null) qp.set('compute_missing', String(computeMissing));
  if (dateFrom != null) qp.set('date_from', String(dateFrom));
  if (dateTo != null) qp.set('date_to', String(dateTo));

  const query = qp.toString() ? `?${qp}` : '';
  return fetchClassified(
    `/options/contract/${encodeURIComponent(collection)}/${encodeURIComponent(contractId)}${query}`,
  );
}

// ---------------------------------------------------------------------------
// 4. Selection resolver
//    GET /api/options/select?q=<JSON-stringified SelectQuery>
//
// selectQuery: {
//   root, date, type,
//   criterion: { kind: 'by_delta'|'by_moneyness'|'by_strike', ... },
//   maturity:  { kind: 'next_third_friday'|'end_of_month'|..., ... },
//   computeMissingForDeltaSelection?: boolean
// }
//
// The entire selectQuery object is JSON-stringified and URL-encoded.
// Backend uses SelectQuery.model_validate_json to parse it, so the
// criterion/maturity discriminated-union shapes must be valid JSON.
// ---------------------------------------------------------------------------

export async function selectOption(selectQuery) {
  const q = encodeURIComponent(JSON.stringify(selectQuery));
  return fetchClassified(`/options/select?q=${q}`);
}

// ---------------------------------------------------------------------------
// 5. Multi-expiration smile snapshot
//    GET /api/options/chain-snapshot
//
// options:
//   date              YYYY-MM-DD string (required)
//   type              'C' | 'P' (default 'C')
//   expirations       array of YYYY-MM-DD strings (required; max 8 per backend guard)
//   field             'iv' | 'delta' (default 'iv')
//   expiration_cycle  optional string (e.g. 'M' / 'W' / 'D') — restricts
//                     the smile to one contract cycle so multi-cycle roots
//                     (notably OPT_SP_500: SPX-monthly + SPXW-weekly) yield
//                     a single point per strike. Omit / null to keep all
//                     cycles (legacy behaviour).
//
// expirations MUST be serialised as repeated query params
// (e.g. expirations=2024-04-19&expirations=2024-05-17), NOT comma-joined.
// URLSearchParams.append is used — NOT set — to preserve all values.
// ---------------------------------------------------------------------------

export async function getChainSnapshot(root, { date, type, expirations, field, expiration_cycle }) {
  const qp = new URLSearchParams();
  qp.set('root', String(root));
  qp.set('date', String(date));
  if (type != null) qp.set('type', String(type));
  // Repeated param — must append each expiration separately.
  if (Array.isArray(expirations)) {
    for (const exp of expirations) {
      qp.append('expirations', String(exp));
    }
  }
  if (field != null) qp.set('field', String(field));
  if (expiration_cycle != null) qp.set('expiration_cycle', String(expiration_cycle));

  return fetchClassified(`/options/chain-snapshot?${qp}`);
}
