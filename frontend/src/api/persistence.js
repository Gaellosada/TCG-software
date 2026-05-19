/**
 * Persistence API client — CRUD for signals and portfolios stored in MongoDB
 * via the backend persistence layer.
 *
 * Endpoint base: /api/persistence/
 *   POST   /signals                  create signal
 *   GET    /signals?category=<cat>   list signals by category (required)
 *   GET    /signals/{id}             get one signal
 *   PUT    /signals/{id}             update signal (full replace)
 *   DELETE /signals/{id}             archive signal (soft-delete → ARCHIVE)
 *
 *   POST   /portfolios               create portfolio
 *   GET    /portfolios?category=<c>  list portfolios by category (required)
 *   GET    /portfolios/{id}          get one portfolio
 *   PUT    /portfolios/{id}          update portfolio (full replace)
 *   DELETE /portfolios/{id}          archive portfolio
 *
 * Category values: "RESEARCH" | "DEV" | "PROD" | "ARCHIVE"
 *
 * This module is intentionally separate from api/signals.js (which wraps the
 * /api/signals/compute endpoint) and api/portfolio.js (which wraps
 * /api/portfolio/compute). Those handle computation; this handles persistence.
 */

const BASE = '/api/persistence';

/** Valid category values. */
export const CATEGORIES = /** @type {const} */ (['RESEARCH', 'DEV', 'PROD', 'ARCHIVE']);

/**
 * Throw a structured error from a non-2xx response.
 * Attaches `.status` and `.body` so callers can distinguish 404 from 422.
 */
async function _handleResponse(res) {
  if (res.ok) return res.json();
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

// ---------------------------------------------------------------------------
// Signals
// ---------------------------------------------------------------------------

/**
 * Create a new persisted signal.
 *
 * Backend defaults inputs / rules / settings / description when omitted,
 * so a minimal payload (id + name + category) creates a valid empty signal.
 *
 * @param {{ id: string, name: string, category: string,
 *   inputs?: Array<object>, rules?: object, settings?: object,
 *   description?: string }} payload
 * @returns {Promise<SignalOut>}
 */
export function createSignal(payload) {
  return _fetch('/signals', { method: 'POST', body: payload });
}

/**
 * List signals in a given category.
 *
 * @param {string} category  One of CATEGORIES.
 * @returns {Promise<Array<SignalOut>>}
 */
export function listSignals(category) {
  return _fetch(`/signals?category=${encodeURIComponent(category)}`);
}

/**
 * Get a single persisted signal by id.
 *
 * @param {string} id
 * @returns {Promise<SignalOut>}
 */
export function getSignal(id) {
  return _fetch(`/signals/${encodeURIComponent(id)}`);
}

/**
 * Full-replace update for a persisted signal. PUT semantics — the
 * caller must supply every field; omitted optional fields default to
 * empty list / dict / string on the backend.
 *
 * @param {string} id
 * @param {{ name: string, category: string,
 *   inputs?: Array<object>, rules?: object, settings?: object,
 *   description?: string }} payload
 * @returns {Promise<SignalOut>}
 */
export function updateSignal(id, payload) {
  return _fetch(`/signals/${encodeURIComponent(id)}`, { method: 'PUT', body: payload });
}

/**
 * Soft-archive a signal (moves it to ARCHIVE category on the server).
 * Returns 204 No Content — the Promise resolves to null on success.
 *
 * @param {string} id
 * @returns {Promise<null>}
 */
export async function archiveSignal(id) {
  const res = await fetch(`${BASE}/signals/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch { /* ignore */ }
    const msg = (body && (body.detail || body.message)) || res.statusText || 'Delete failed';
    const err = new Error(msg);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Portfolios
// ---------------------------------------------------------------------------

/**
 * Create a new persisted portfolio.
 *
 * @param {{ id: string, name: string, category: string,
 *   legs?: Array<object>, rebalance?: string }} payload
 * @returns {Promise<PortfolioOut>}
 */
export function createPortfolio(payload) {
  return _fetch('/portfolios', { method: 'POST', body: payload });
}

/**
 * List portfolios in a given category.
 *
 * @param {string} category  One of CATEGORIES.
 * @returns {Promise<Array<PortfolioOut>>}
 */
export function listPortfolios(category) {
  return _fetch(`/portfolios?category=${encodeURIComponent(category)}`);
}

/**
 * Get a single persisted portfolio by id.
 *
 * @param {string} id
 * @returns {Promise<PortfolioOut>}
 */
export function getPortfolio(id) {
  return _fetch(`/portfolios/${encodeURIComponent(id)}`);
}

/**
 * Full-replace update for a persisted portfolio. PUT semantics — the
 * caller must supply every field; omitted optional fields default to
 * empty list / "none" on the backend.
 *
 * @param {string} id
 * @param {{ name: string, category: string,
 *   legs?: Array<object>, rebalance?: string }} payload
 * @returns {Promise<PortfolioOut>}
 */
export function updatePortfolio(id, payload) {
  return _fetch(`/portfolios/${encodeURIComponent(id)}`, { method: 'PUT', body: payload });
}

/**
 * Soft-archive a portfolio.
 * Returns 204 No Content — the Promise resolves to null on success.
 *
 * @param {string} id
 * @returns {Promise<null>}
 */
export async function archivePortfolio(id) {
  const res = await fetch(`${BASE}/portfolios/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) {
    let body = null;
    try { body = await res.json(); } catch { /* ignore */ }
    const msg = (body && (body.detail || body.message)) || res.statusText || 'Delete failed';
    const err = new Error(msg);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return null;
}
