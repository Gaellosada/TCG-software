// Normalize a backend error response into the structured shape the
// chart / results panels render. New envelope: {error_type, message, traceback?}.
// Legacy shapes ({detail: "..."} or {message: "..."}) default to
// error_type='validation' — same meaning as HTTP 400.
import { coerceErrorType } from '../pages/Indicators/errorTaxonomy';

export function normalizeErrorEnvelope(body, fallbackStatusText) {
  if (!body || typeof body !== 'object') {
    return { error_type: 'validation', message: fallbackStatusText || 'Request failed' };
  }
  const error_type = coerceErrorType(body.error_type);
  const message = (typeof body.message === 'string' && body.message)
    || (typeof body.detail === 'string' && body.detail)
    || fallbackStatusText
    || 'Request failed';
  const out = { error_type, message };
  if (typeof body.traceback === 'string' && body.traceback) {
    out.traceback = body.traceback;
  }
  return out;
}
