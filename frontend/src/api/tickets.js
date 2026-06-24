/**
 * Tickets API client — CRUD for the bespoke ``tickets`` feature.
 *
 * A ticket is a single free-text note a user records when they hit an issue.
 * It is stored in its own minimal 3-column table (``id, text, created_at``)
 * via a SELF-CONTAINED backend path — NOT the uniform id/type/category/locked
 * doc machinery the other persistence resources use. There is therefore no
 * category, no lock, no soft-delete: delete is a permanent HARD delete.
 *
 * Endpoint base: /api/persistence/
 *   POST   /tickets        create  → 201 {id, text, created_at}
 *   GET    /tickets        list    → 200 [{id, text, created_at}, …] newest-first
 *   PUT    /tickets/{id}   update  → 200 {id, text, created_at} · 404 if missing
 *   DELETE /tickets/{id}   delete  → 204 · 404 if missing · HARD delete
 *
 * ``created_at`` is an ISO-8601 timestamp string. ``text`` is validated
 * server-side (non-empty after trim, max 10000 chars); a violation comes back
 * as HTTP 400 with the project envelope ``{error_type, message}`` (the global
 * request-validation handler maps Pydantic 422 → 400). A 404 carries the
 * standard FastAPI ``{detail}`` shape.
 *
 * This mirrors the ``api/persistence.js`` wrapper style: its own ``_fetch`` /
 * ``_handleResponse`` so 204 No Content is handled and ``.status`` / ``.body``
 * are attached to the thrown Error. Errors are surfaced via the shared
 * ``describePersistenceError`` helper (re-exported here) so the Tickets page
 * reports failures exactly as the other persistence pages do.
 */

import { API_BASE } from './base';
import { describePersistenceError } from './persistence';

const BASE = `${API_BASE}/persistence`;

// Re-export so the Tickets page imports its error formatter from one place
// (the api layer) rather than reaching across to api/persistence.js.
export { describePersistenceError };

/**
 * Throw a structured error from a non-2xx response. Attaches ``.status`` and
 * ``.body`` so callers can distinguish 404 from a 400 validation error.
 * Reads ``detail`` (FastAPI HTTPException) OR ``message`` (project envelope)
 * for the human-facing message — covering both shapes the backend emits.
 */
async function _handleResponse(res) {
  if (res.ok) {
    // 204 No Content has no body — guard against res.json() throwing.
    if (res.status === 204) return null;
    return res.json();
  }
  let body = null;
  try { body = await res.json(); } catch { /* ignore */ }
  const msg = (body && (body.detail || body.message)) || res.statusText || 'Request failed';
  const err = new Error(msg);
  err.status = res.status;
  err.body = body;
  throw err;
}

async function _fetch(path, options = {}) {
  const { body, ...rest } = options;
  const init = {
    ...rest,
    headers: { 'Content-Type': 'application/json', ...(rest.headers || {}) },
  };
  if (body !== undefined) init.body = typeof body === 'string' ? body : JSON.stringify(body);
  const res = await fetch(`${BASE}${path}`, init);
  return _handleResponse(res);
}

/**
 * @typedef {{ id: string, text: string, created_at: string }} TicketOut
 */

/**
 * List all tickets, newest first (``created_at`` DESC — ordered by the
 * backend, so the array is rendered in order without re-sorting).
 *
 * @returns {Promise<Array<TicketOut>>}
 */
export function listTickets() {
  return _fetch('/tickets');
}

/**
 * Create a ticket. ``id`` (uuid4 hex) and ``created_at`` (UTC now) are
 * generated server-side; the body carries only ``text``.
 *
 * @param {string} text
 * @returns {Promise<TicketOut>}
 */
export function createTicket(text) {
  return _fetch('/tickets', { method: 'POST', body: { text } });
}

/**
 * In-place edit of a ticket's ``text``. Rejects with a ``.status === 404``
 * Error when the id is unknown.
 *
 * @param {string} id
 * @param {string} text
 * @returns {Promise<TicketOut>}
 */
export function updateTicket(id, text) {
  return _fetch(`/tickets/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: { text },
  });
}

/**
 * HARD-delete a ticket (permanent — the row is physically removed, NOT
 * soft-deleted). Returns 204 No Content → resolves to ``null`` on success;
 * rejects with a ``.status === 404`` Error when the id is unknown.
 *
 * @param {string} id
 * @returns {Promise<null>}
 */
export function deleteTicket(id) {
  return _fetch(`/tickets/${encodeURIComponent(id)}`, { method: 'DELETE' });
}
