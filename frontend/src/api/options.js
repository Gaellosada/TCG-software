import { fetchApi } from './client';
import { classifyFetchError, FetchError } from '../utils/fetchError';

// Re-throw any error as a classified ``FetchError``. Preserves AbortError
// for callers that need abort semantics. Shared by both GET helpers
// (``fetchClassified``) and the POST ``resolveOptionStream`` function.
function rethrowClassified(err) {
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

// Thin wrapper around ``fetchApi`` that re-throws network/HTTP failures
// as a ``FetchError`` with a classified ``kind``. Mirrors the same helper
// defined in ``data.js`` — both files use an identical local copy so neither
// depends on the other.
async function fetchClassified(path, options = {}) {
  try {
    return await fetchApi(path, options);
  } catch (err) {
    rethrowClassified(err);
  }
}

// ---------------------------------------------------------------------------
// 1. List option roots
//    GET /api/options/roots
//    Returns: { roots: OptionRootInfo[] }
// ---------------------------------------------------------------------------

export async function getOptionRoots({ signal } = {}) {
  return fetchClassified('/options/roots', signal ? { signal } : {});
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
// 1c. Trade-date coverage (data span) for a root
//     GET /api/options/coverage?root=OPT_SP_500
//     Returns: { root, start: 'YYYY-MM-DD'|null, end: 'YYYY-MM-DD'|null }
//     Used by the portfolio editor to resolve an option leg's real available
//     date range (so an option-only portfolio's slider floors at the option
//     collection's true history, not an artificial recent default).
// ---------------------------------------------------------------------------

export async function getOptionCoverage(root) {
  const qp = new URLSearchParams({ root: String(root) });
  return fetchClassified(`/options/coverage?${qp}`);
}

// ---------------------------------------------------------------------------
// 2. Chain query
//    GET /api/options/chain
//    Returns: ChainResponse
//
// params:
//   date            YYYY-MM-DD string (required)
//   type            'C' | 'P' | 'both' (default 'both')
//   expirationMin   YYYY-MM-DD string (required)
//   expirationMax   YYYY-MM-DD string (required)
//   strikeMin       number (optional)
//   strikeMax       number (optional)
//   computeMissing  boolean (optional, default false)
//   expirationCycle optional string ('M' / 'W' / 'D' / ...) — restricts the
//                   chain to one contract cycle (for the SPX-monthly +
//                   SPXW-weekly overlap on OPT_SP_500). Omit / null to keep
//                   all cycles. Empty strings are NOT sent on the wire —
//                   the backend coerces them anyway, but we drop them here
//                   for cleaner URLs and to mirror the smile-dropdown contract.
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
    expirationCycle,
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
  if (expirationCycle != null && String(expirationCycle).trim() !== '') {
    qp.set('expiration_cycle', String(expirationCycle));
  }

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

export async function selectOption(selectQuery, { signal } = {}) {
  const q = encodeURIComponent(JSON.stringify(selectQuery));
  return fetchClassified(`/options/select?q=${q}`, signal ? { signal } : {});
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

// ---------------------------------------------------------------------------
// 6. Resolve option stream(s)
//    POST /api/options/stream
//
// streams: Array<{ ref: OptionStreamRef, label: string }>
// start:   YYYY-MM-DD string
// end:     YYYY-MM-DD string
// task_id: optional UUID for progress polling
//
// Returns: { dates: string[], streams: { [label]: { values: number[], diagnostics: (string|null)[] } } }
//
// Progress: GET /api/options/stream/progress/{task_id}
//   Returns: { done: number, total: number, fraction: number }
// ---------------------------------------------------------------------------

// Ensure each option_stream ref carries the ``roll_offset`` (the unified
// {value, unit} ROLL-EARLY object, default {0, days}) before it goes on the
// wire. The picker emits it, but legacy / hand-built refs may omit it; the
// backend defaults it too, but we send it explicitly so the request body is
// unambiguous (and testable). A shipped bare int is still accepted by the
// backend (read as days), but we normalise to the object here.
//
// NOTE: option streams carry NO back-adjustment (ratio/difference are ill-posed
// for option premia), so there is no ``adjustment`` field. A stray ``adjustment``
// key on a legacy ref is harmless — the backend ignores unknown fields. "Roll at
// end of month" is the EndOfMonth maturity, not a roll_offset value.
function withOptionStreamDefaults(streams) {
  if (!Array.isArray(streams)) return streams;
  return streams.map((entry) => {
    const ref = entry && entry.ref;
    if (
      ref
      && ref.type === 'option_stream'
      && ref.roll_offset === undefined
    ) {
      return {
        ...entry,
        ref: {
          ...ref,
          roll_offset: { value: 0, unit: 'days' },
        },
      };
    }
    return entry;
  });
}

export async function resolveOptionStream(streams, start, end, { signal, onProgress } = {}) {
  const normalizedStreams = withOptionStreamDefaults(streams);
  const taskId = typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

  let progressInterval = null;
  if (onProgress) {
    progressInterval = setInterval(async () => {
      try {
        const prog = await fetchApi(`/options/stream/progress/${taskId}`);
        if (prog && typeof prog.fraction === 'number') {
          onProgress(prog.fraction);
        }
      } catch {
        // Progress polling is best-effort — swallow errors.
      }
    }, 250);
  }

  try {
    const body = { streams: normalizedStreams, start, end, task_id: taskId };
    const result = await fetchApi('/options/stream', {
      method: 'POST',
      body: JSON.stringify(body),
      signal,
    });
    return result;
  } catch (err) {
    rethrowClassified(err);
  } finally {
    if (progressInterval) clearInterval(progressInterval);
  }
}

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
