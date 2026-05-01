// Normalize a backend error response into the structured shape the
// chart / results panels render. New envelope: {error_type, message, traceback?}.
// Legacy shapes ({detail: "..."} or {message: "..."}) default to
// error_type='validation' — same meaning as HTTP 400.
//
// Typed ``error_code`` field
// --------------------------
// Some endpoints (e.g. /api/indicators/compute) return a typed
// ``error_code`` string identifying a stable failure mode the frontend
// must route on. When present and recognised, it overrides the
// ``error_type`` field — Sign 10: route on the typed code, never on
// the human-readable message. The original ``error_code`` and any
// auxiliary fields (e.g. ``accepted_asset_types``, ``asset_type``,
// ``indicator_id``) are preserved on the returned envelope so
// downstream renderers can display the canonical accepted list.
import { coerceErrorType, errorCodeToType } from '../pages/Indicators/errorTaxonomy';

export function normalizeErrorEnvelope(body, fallbackStatusText) {
  if (!body || typeof body !== 'object') {
    return { error_type: 'validation', message: fallbackStatusText || 'Request failed' };
  }
  // Typed error_code routing — takes precedence when recognised.
  const codeMapped = errorCodeToType(body.error_code);
  const error_type = codeMapped || coerceErrorType(body.error_type);
  const message = (typeof body.message === 'string' && body.message)
    || (typeof body.detail === 'string' && body.detail)
    || fallbackStatusText
    || 'Request failed';
  const out = { error_type, message };
  if (typeof body.traceback === 'string' && body.traceback) {
    out.traceback = body.traceback;
  }
  if (typeof body.error_code === 'string' && body.error_code) {
    out.error_code = body.error_code;
  }
  // Preserve INDICATOR_INCOMPATIBLE_ASSET context fields verbatim so
  // the chart / banner can display the backend-canonical accepted list
  // rather than a stale frontend-side copy.
  if (Array.isArray(body.accepted_asset_types)) {
    out.accepted_asset_types = body.accepted_asset_types.slice();
  }
  if (typeof body.asset_type === 'string' && body.asset_type) {
    out.asset_type = body.asset_type;
  }
  if (typeof body.indicator_id === 'string' && body.indicator_id) {
    out.indicator_id = body.indicator_id;
  }
  return out;
}
