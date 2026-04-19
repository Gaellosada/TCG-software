// Local persistence for Indicators page state.
//
// All direct ``localStorage`` access for indicators lives in this module —
// other modules MUST go through load/save here. This keeps the storage
// key + schema version in one place and makes the persistence layer
// easy to mock in tests.
//
// Schema v1:
//   {
//     "version": 1,
//     "indicators": [                    // user-authored only
//       { "id", "name", "code", "doc", "params", "seriesMap" }
//     ],
//     "defaultState": {                  // per-session overlay for readonly defaults
//       "<defaultId>": { "params", "seriesMap" }
//     }
//   }
//
// ``doc`` (Wave 1a, indicator-doc-tab) is a markdown string owned by the
// user for custom indicators. Default indicators' docs live in the
// ``DEFAULT_INDICATORS`` registry and are NEVER persisted here. A
// missing or non-string ``doc`` on read is coerced to the empty string
// so legacy payloads (pre-``doc``) load cleanly — no schema bump, no
// migration step.

import { INDICATORS_STORAGE_KEY } from './storageKeys';

export const SCHEMA_VERSION = 1;

function getStorage() {
  try {
    if (typeof globalThis !== 'undefined' && globalThis.localStorage) {
      return globalThis.localStorage;
    }
  } catch {
    // Accessing localStorage can throw in some sandboxes (e.g. strict CSP).
  }
  return null;
}

export function loadState() {
  const empty = { indicators: [], defaultState: {} };
  const ls = getStorage();
  if (!ls) return empty;
  let raw;
  try {
    raw = ls.getItem(INDICATORS_STORAGE_KEY);
  } catch {
    return empty;
  }
  if (!raw) return empty;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return empty;
  }
  if (!parsed || typeof parsed !== 'object') return empty;
  if (parsed.version !== SCHEMA_VERSION) return empty;

  // Sanitise indicators[]: strip any entry flagged readonly (defensive —
  // defaults must never masquerade as user indicators).
  const rawIndicators = Array.isArray(parsed.indicators) ? parsed.indicators : [];
  const indicators = [];
  for (const ind of rawIndicators) {
    if (!ind || typeof ind !== 'object') continue;
    if (ind.readonly) continue;
    if (typeof ind.id !== 'string' || !ind.id) continue;
    indicators.push({
      id: ind.id,
      name: typeof ind.name === 'string' ? ind.name : 'Untitled',
      code: typeof ind.code === 'string' ? ind.code : '',
      doc: typeof ind.doc === 'string' ? ind.doc : '',
      params: (ind.params && typeof ind.params === 'object') ? ind.params : {},
      seriesMap: (ind.seriesMap && typeof ind.seriesMap === 'object') ? ind.seriesMap : {},
    });
  }

  const rawDefaults = (parsed.defaultState && typeof parsed.defaultState === 'object')
    ? parsed.defaultState
    : {};
  const defaultState = {};
  for (const [id, entry] of Object.entries(rawDefaults)) {
    if (!entry || typeof entry !== 'object') continue;
    defaultState[id] = {
      params: (entry.params && typeof entry.params === 'object') ? entry.params : {},
      seriesMap: (entry.seriesMap && typeof entry.seriesMap === 'object') ? entry.seriesMap : {},
    };
  }

  return { indicators, defaultState };
}

/**
 * Persist the Indicators page state. ``state`` must have the shape
 * ``{indicators, defaultState}``. Read-only indicators are stripped from
 * ``indicators[]`` before writing (belt-and-braces — the caller should
 * already be filtering).
 */
export function saveState(state) {
  const ls = getStorage();
  if (!ls) return;
  const indicators = Array.isArray(state?.indicators) ? state.indicators : [];
  const defaultState = (state?.defaultState && typeof state.defaultState === 'object')
    ? state.defaultState
    : {};
  const payload = {
    version: SCHEMA_VERSION,
    indicators: indicators
      .filter((ind) => ind && !ind.readonly && typeof ind.id === 'string')
      .map((ind) => ({
        id: ind.id,
        name: typeof ind.name === 'string' ? ind.name : 'Untitled',
        code: typeof ind.code === 'string' ? ind.code : '',
        doc: typeof ind.doc === 'string' ? ind.doc : '',
        params: (ind.params && typeof ind.params === 'object') ? ind.params : {},
        seriesMap: (ind.seriesMap && typeof ind.seriesMap === 'object') ? ind.seriesMap : {},
      })),
    defaultState,
  };
  try {
    ls.setItem(INDICATORS_STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // Quota / access errors — nothing to do, session state keeps working.
  }
}

