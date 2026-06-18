/**
 * Persistence API client — CRUD for signals, portfolios, and indicators
 * stored in MongoDB via the backend persistence layer.
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
 *   POST   /indicators               create indicator
 *   GET    /indicators               list active indicators (no category)
 *   GET    /indicators/{id}          get one indicator
 *   PUT    /indicators/{id}          update indicator (full replace)
 *   DELETE /indicators/{id}          archive indicator (sets deleted=true)
 *
 * Category values: "RESEARCH" | "DEV" | "PROD" | "ARCHIVE"
 *
 * This module is intentionally separate from api/signals.js (which wraps the
 * /api/signals/compute endpoint) and api/portfolio.js (which wraps
 * /api/portfolio/compute). Those handle computation; this handles persistence.
 */

import { API_BASE } from './base';

const BASE = `${API_BASE}/persistence`;

/** Valid category values. */
export const CATEGORIES = /** @type {const} */ (['RESEARCH', 'DEV', 'PROD', 'ARCHIVE']);

/**
 * Throw a structured error from a non-2xx response.
 * Attaches `.status` and `.body` so callers can distinguish 404 from 422.
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
 * Categorise an Error from a persistence call into a short user-facing
 * label, distinguishing user-actionable (4xx) from server (5xx) errors.
 * Returns the original message if the error has no ``.status`` (network
 * error / AbortError / unknown).
 */
export function describePersistenceError(err) {
  if (!err) return 'Unknown error';
  if (err.name === 'AbortError') return 'Cancelled';
  const status = err.status;
  const msg = err.message || String(err);
  if (typeof status !== 'number') return msg;
  if (status === 409) return `Conflict (409): ${msg}`;
  if (status === 413) return `Payload too large (413): ${msg}`;
  if (status === 422) return `Validation error (422): ${msg}`;
  if (status >= 400 && status < 500) return `Client error (${status}): ${msg}`;
  if (status >= 500) return `Server error (${status}): ${msg}`;
  return msg;
}

/**
 * Detect whether a persistence Error represents a locked-document write
 * rejection. The backend guards writes to a locked doc with HTTP 423 Locked
 * (guardrail Sign 6); the ``.status`` is attached by ``_handleResponse`` /
 * ``_archive``. Callers use this to flip their LOCAL ``locked`` flag → the
 * editor goes read-only with the normal lock banner instead of surfacing a
 * generic error toast.
 *
 * @param {*} err
 * @returns {boolean}
 */
export function isLockedError(err) {
  return !!err && err.status === 423;
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
export function updateSignal(id, payload, options = {}) {
  const { signal } = options;
  return _fetch(`/signals/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: payload,
    ...(signal ? { signal } : {}),
  });
}

/**
 * Soft-archive a signal (moves it to ARCHIVE category on the server).
 * Returns 204 No Content — the Promise resolves to null on success.
 *
 * @param {string} id
 * @returns {Promise<null>}
 */
/**
 * Shared archive helper — all archive endpoints follow the same pattern:
 * DELETE to the given path, expect 204 on success. Does NOT use ``_fetch``
 * because the response body is intentionally empty on success.
 */
async function _archive(path) {
  const res = await fetch(`${BASE}${path}`, {
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

export function archiveSignal(id) {
  return _archive(`/signals/${encodeURIComponent(id)}`);
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
export function updatePortfolio(id, payload, options = {}) {
  const { signal } = options;
  return _fetch(`/portfolios/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: payload,
    ...(signal ? { signal } : {}),
  });
}

/**
 * Soft-archive a portfolio.
 * Returns 204 No Content — the Promise resolves to null on success.
 *
 * @param {string} id
 * @returns {Promise<null>}
 */
export function archivePortfolio(id) {
  return _archive(`/portfolios/${encodeURIComponent(id)}`);
}

// ---------------------------------------------------------------------------
// Indicators
// ---------------------------------------------------------------------------

/**
 * Create a new persisted indicator.
 *
 * @param {{ id: string, name: string, definition: object }} payload
 * @returns {Promise<IndicatorOut>}
 */
export function createIndicator(payload) {
  return _fetch('/indicators', { method: 'POST', body: payload });
}

/**
 * List all active (non-deleted) indicators.
 * No category filter — indicators use a flat list.
 *
 * @returns {Promise<Array<IndicatorOut>>}
 */
export function listIndicators() {
  return _fetch('/indicators');
}

/**
 * Get a single persisted indicator by id.
 *
 * @param {string} id
 * @returns {Promise<IndicatorOut>}
 */
export function getIndicator(id) {
  return _fetch(`/indicators/${encodeURIComponent(id)}`);
}

/**
 * Full-replace update for a persisted indicator.
 *
 * @param {string} id
 * @param {{ name: string, definition: object }} payload
 * @param {{ signal?: AbortSignal }} [options]
 * @returns {Promise<IndicatorOut>}
 */
export function updateIndicator(id, payload, options = {}) {
  const { signal } = options;
  return _fetch(`/indicators/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: payload,
    ...(signal ? { signal } : {}),
  });
}

/**
 * Soft-archive an indicator (sets deleted=true on the server).
 * Returns 204 No Content — the Promise resolves to null on success.
 *
 * @param {string} id
 * @returns {Promise<null>}
 */
export function archiveIndicator(id) {
  return _archive(`/indicators/${encodeURIComponent(id)}`);
}

// ---------------------------------------------------------------------------
// Lock / Unlock
// ---------------------------------------------------------------------------

/**
 * Set the ``locked`` flag on a persisted indicator.
 * Sends ``PUT /api/persistence/indicators/{id}/lock`` with body ``{ locked }``.
 *
 * @param {string}  id
 * @param {boolean} locked
 * @returns {Promise<IndicatorOut>}  updated doc returned by the server
 */
export function setIndicatorLocked(id, locked) {
  return _fetch(`/indicators/${encodeURIComponent(id)}/lock`, {
    method: 'PUT',
    body: { locked },
  });
}

/**
 * Set the ``locked`` flag on a persisted signal.
 * Sends ``PUT /api/persistence/signals/{id}/lock`` with body ``{ locked }``.
 *
 * @param {string}  id
 * @param {boolean} locked
 * @returns {Promise<SignalOut>}  updated doc returned by the server
 */
export function setSignalLocked(id, locked) {
  return _fetch(`/signals/${encodeURIComponent(id)}/lock`, {
    method: 'PUT',
    body: { locked },
  });
}

/**
 * Set the ``locked`` flag on a persisted portfolio.
 * Sends ``PUT /api/persistence/portfolios/{id}/lock`` with body ``{ locked }``.
 *
 * @param {string}  id
 * @param {boolean} locked
 * @returns {Promise<PortfolioOut>}  updated doc returned by the server
 */
export function setPortfolioLocked(id, locked) {
  return _fetch(`/portfolios/${encodeURIComponent(id)}/lock`, {
    method: 'PUT',
    body: { locked },
  });
}

// ---------------------------------------------------------------------------
// Baskets
// ---------------------------------------------------------------------------

/**
 * Create a new persisted basket.
 *
 * Leg shape is polymorphic per iter-3: each leg carries an `instrument`
 * sub-object discriminated by `type` (`spot` | `continuous` |
 * `option_stream`) plus a signed non-zero `weight`. The envelope
 * `asset_class` determines which `instrument.type` is permitted
 * (`equity`/`index` → spot; `future` → continuous; `option` →
 * option_stream); the BE rejects mismatches with 400.
 *
 * @param {{
 *   id: string,
 *   name: string,
 *   category: string,
 *   asset_class: 'equity' | 'index' | 'future' | 'option',
 *   legs?: Array<{instrument: object, weight: number}>
 * }} payload
 * @returns {Promise<BasketOut>}
 */
export function createBasket(payload) {
  return _fetch('/baskets', { method: 'POST', body: payload });
}

/**
 * List baskets in a given category.
 *
 * @param {string} category  One of CATEGORIES.
 * @returns {Promise<Array<BasketOut>>}
 */
export function listBaskets(category) {
  return _fetch(`/baskets?category=${encodeURIComponent(category)}`);
}

/**
 * Get a single persisted basket by id.
 *
 * @param {string} id
 * @returns {Promise<BasketOut>}
 */
export function getBasket(id) {
  return _fetch(`/baskets/${encodeURIComponent(id)}`);
}

/**
 * Full-replace update for a persisted basket. PUT semantics — caller
 * must supply every field; the backend defaults ``legs`` to an empty
 * list when omitted.
 *
 * Leg shape matches {@link createBasket}: each leg carries an
 * `instrument` sub-object discriminated by `type` (`spot` | `continuous`
 * | `option_stream`) plus a signed non-zero `weight`. Envelope
 * `asset_class` gates which `instrument.type` is permitted per leg.
 *
 * @param {string} id
 * @param {{
 *   name: string,
 *   category: string,
 *   asset_class: 'equity' | 'index' | 'future' | 'option',
 *   legs?: Array<{instrument: object, weight: number}>
 * }} payload
 * @param {{ signal?: AbortSignal }} [options]
 * @returns {Promise<BasketOut>}
 */
export function updateBasket(id, payload, options = {}) {
  const { signal } = options;
  return _fetch(`/baskets/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: payload,
    ...(signal ? { signal } : {}),
  });
}

/**
 * Soft-archive a basket (moves it to ARCHIVE category on the server).
 * Returns 204 No Content — the Promise resolves to null on success.
 *
 * @param {string} id
 * @returns {Promise<null>}
 */
export function archiveBasket(id) {
  return _archive(`/baskets/${encodeURIComponent(id)}`);
}
