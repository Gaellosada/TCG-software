// Single source of truth for Indicators-page error-kind plumbing.
//
// Three independent flows used to each carry their own copy of this
// mapping; this module consolidates them:
//
//   1. ``fetchError.classifyFetchError`` — emits a ``kind`` describing
//      the network/HTTP failure.
//   2. ``IndicatorsPage.runIndicator`` — maps that ``kind`` into the
//      ``error_type`` the error card consumes.
//   3. ``IndicatorChart.ErrorCard`` — maps ``error_type`` into a human
//      heading.
//
// Each entry in ``INDICATOR_ERROR_TYPES`` is the canonical list of
// chart-side error types. ``HEADINGS`` must have an entry for every
// one of them (enforced by ``errorTaxonomy.test.js``).
//
// Backend typed error_codes
// -------------------------
// Some error responses carry a typed ``error_code`` field that lets the
// frontend route on a stable, machine-parseable signal rather than a
// human message regex (Sign 10 — no silent failures, no string-match
// routing). The known codes are mapped to a chart-side ``error_type``
// via ``ERROR_CODE_TO_TYPE``. Any code not present here falls through
// to whatever ``error_type`` the backend already chose; the code is
// retained on the structured envelope so downstream renderers (banner,
// run-gate) can still discriminate.

/** Canonical chart-side error types. Anything not in this set falls back to 'generic'. */
export const INDICATOR_ERROR_TYPES = Object.freeze([
  'validation',
  'runtime',
  'data',
  'network',
  'offline',
  'incompatible_asset',
  'tautological_option_stream',
  'stream_unavailable_for_root',
  'options_data_access_error',
]);

/** Human-facing headings for each error type. */
export const HEADINGS = Object.freeze({
  validation: 'Invalid indicator',
  runtime: 'Indicator error',
  data: 'Data error',
  network: "Couldn't reach the server",
  offline: "You're offline",
  incompatible_asset: 'Indicator not compatible with this asset',
  tautological_option_stream: 'Tautological selection',
  stream_unavailable_for_root: 'Stream unavailable for root',
  options_data_access_error: 'Options data unavailable',
});

/**
 * Subtitles / tooltips for option-stream-specific error types. The
 * canonical phrasing comes from the backend's typed-422 detail field;
 * when the backend message is absent we fall back to these strings.
 *
 * Keep in sync with the backend response body shapes documented in
 * Wave 2a (`tautological_option_stream_response`,
 * `stream_unavailable_for_root_response`).
 */
export const SUBTITLES = Object.freeze({
  tautological_option_stream:
    "selection=by_delta with stream='delta' returns the target delta by construction.",
  stream_unavailable_for_root:
    'Greek streams (gamma, vega, theta) are not available on this option root.',
});

/**
 * Sentinel returned by ``fetchKindToErrorType`` when the caller should
 * suppress rendering entirely (AbortController cancellations).
 */
export const ABORTED = 'aborted';

/**
 * Typed backend ``error_code`` → chart-side ``error_type`` map.
 *
 * The frontend MUST route on these codes (typed strings) and never on
 * the human-readable ``message`` field. Add entries here when the
 * backend grows new typed codes — the keys are stable contract values
 * shared between client and server.
 */
export const ERROR_CODE_TO_TYPE = Object.freeze({
  INDICATOR_INCOMPATIBLE_ASSET: 'incompatible_asset',
  TAUTOLOGICAL_OPTION_STREAM: 'tautological_option_stream',
  STREAM_UNAVAILABLE_FOR_ROOT: 'stream_unavailable_for_root',
});

/**
 * Map a ``classifyFetchError`` ``kind`` onto the chart-side ``error_type``.
 *
 * Returns ``ABORTED`` for user-cancelled requests — callers MUST skip
 * rendering an error card in that case.
 */
export function fetchKindToErrorType(kind) {
  switch (kind) {
    case 'aborted':  return ABORTED;
    case 'offline':  return 'offline';
    case 'network':
    case 'server':   return 'network';
    case 'client':   return 'validation';
    default:         return 'runtime';
  }
}

const VALID_ERROR_TYPE_SET = new Set(INDICATOR_ERROR_TYPES);

/**
 * Coerce an arbitrary ``error_type`` from a backend envelope into a
 * known value, defaulting to ``'validation'`` (same semantics as HTTP
 * 400 — shape-of-input errors).
 */
export function coerceErrorType(value) {
  return VALID_ERROR_TYPE_SET.has(value) ? value : 'validation';
}

/**
 * Map a typed backend ``error_code`` onto a chart-side ``error_type``.
 * Returns ``null`` for unknown / missing codes so the caller can fall
 * back to the legacy ``error_type`` field.
 */
export function errorCodeToType(code) {
  if (typeof code !== 'string' || code.length === 0) return null;
  return ERROR_CODE_TO_TYPE[code] || null;
}
