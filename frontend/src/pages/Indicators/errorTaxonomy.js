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

/** Canonical chart-side error types. Anything not in this set falls back to 'generic'. */
export const INDICATOR_ERROR_TYPES = Object.freeze([
  'validation',
  'runtime',
  'data',
  'network',
  'offline',
]);

/** Human-facing headings for each error type. */
export const HEADINGS = Object.freeze({
  validation: 'Invalid indicator',
  runtime: 'Indicator error',
  data: 'Data error',
  network: "Couldn't reach the server",
  offline: "You're offline",
});

/**
 * Sentinel returned by ``fetchKindToErrorType`` when the caller should
 * suppress rendering entirely (AbortController cancellations).
 */
export const ABORTED = 'aborted';

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
