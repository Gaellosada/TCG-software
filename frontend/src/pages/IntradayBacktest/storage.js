// Local persistence for the Intraday Backtest page — saved-simulation configs.
//
// SCOPE: only the SIMULATION CONFIG is stored in localStorage (form + entry +
// exit + hedge + custom-day rows). Results are NEVER stored here — they come
// from the backend result cache (see api/intradayBacktest cache/get). This keeps
// the browser store small and the source of truth for results on the backend.
//
// All direct ``localStorage`` access for this page lives in THIS module. Mirrors
// the discipline of ``pages/Signals/storage.js``:
//   - single-key JSON blob (one object holding a LIST of saved sims),
//   - a ``SCHEMA_VERSION`` + an explicit migration hook (identity for v1),
//   - null-safe ``getStorage()`` (sandbox / quota safe — every access try/caught),
//   - field-local sanitisation of every loaded config.
//
// Schema v1:
//   {
//     "version": 1,
//     "sims": [
//       {
//         "id":      <uuid>,        // stable, generated on first save
//         "name":    <string>,     // user label; unique within the list
//         "savedAt": <ISO string>, // last write time
//         "config":  {             // the exact page inputs (no results)
//           "form":       { start_date, end_date, expiry_mode, dte, straddle_side },
//           "entry":      <EntryExitModule value>,
//           "exit":       <EntryExitModule value>,
//           "hedge":      <HedgeModule value>,
//           "customDays": [ { date, exclude, expanded, entry, exit } ]
//         }
//       }
//     ]
//   }
//
// The ``config`` sub-object is produced/consumed ONLY through the pure
// ``serializeConfig`` / ``deserializeConfig`` boundary so a FUTURE migration to
// the backend persistence layer (``tcg.persistence``) can reuse the exact same
// serialization without touching the page. That migration is intentionally NOT
// implemented here.

import {
  defaultEntryModule,
  defaultExitModule,
} from './EntryExitModule';
import { defaultHedgeModule } from './HedgeModule';

/** Current schema version. Bump + add a migration step when the shape changes. */
export const SCHEMA_VERSION = 1;

/** Single localStorage key — namespaced ``tcg.intraday.*`` (no collisions). */
export const INTRADAY_SIMS_KEY = 'tcg.intraday.sims.v1';

/**
 * Default scalar form fields. Exported so the page has ONE source of truth for
 * the form defaults (the page imports this as ``DEFAULT_FORM``). Kept here (not
 * in the page) so ``storage.js`` has no dependency on the page component and the
 * import graph stays acyclic.
 */
export const DEFAULT_FORM = Object.freeze({
  start_date: '',
  end_date: '',
  expiry_mode: '0DTE',
  dte: 0,
  straddle_side: 'long',
  // P0.2 transaction-cost model: default OFF (mid fills). ``cost_fallback_pts``
  // is the fixed per-side cost (index points) charged when a fill bar has no
  // two-sided quote.
  cost_enabled: false,
  cost_fallback_pts: 0,
  // F2.2 regime-driven side: default OFF (every day trades the static
  // straddle_side, byte-identical to the pre-feature baseline). When on, each
  // day's side is resolved from the vol-regime cascade (backwardation ladder +
  // low-vol floor + VVIX gate) as-of the prior daily close; a flat decision
  // skips the day. All thresholds are configurable, none hardcoded.
  regime_side_enabled: false,
  regime_hvol_tolerance: 0,
  regime_extremely_low_h20: 0,
  regime_vvix_gate_enabled: false,
  regime_vvix_gate_level: 0,
  // F3.2 date-allowlist entry mode: default OFF (every eligible day trades,
  // byte-identical baseline). When on, ONLY the resolved dates trade — the union
  // of the selected event types (FOMC/NFP/CPI, curated F3.1) and any explicit
  // dates. The DISTINCT OPPOSITE of custom_days (which excludes).
  allowlist_enabled: false,
  allowlist_event_types: [],
  allowlist_dates: [],
});

// The valid curated event types (mirrors tcg.types.event_calendar.EVENT_TYPES).
export const ALLOWLIST_EVENT_TYPES = Object.freeze(['FOMC', 'NFP', 'CPI']);

function isObj(x) {
  return x !== null && typeof x === 'object' && !Array.isArray(x);
}

function getStorage() {
  try {
    if (typeof globalThis !== 'undefined' && globalThis.localStorage) {
      return globalThis.localStorage;
    }
  } catch {
    // sandbox — localStorage access can throw
  }
  return null;
}

/** Generate a stable id. Uses crypto.randomUUID when available. */
function newId() {
  try {
    if (typeof globalThis !== 'undefined'
        && globalThis.crypto
        && typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID();
    }
  } catch {
    // fall through
  }
  return `sim-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** JSON-safe deep clone. Returns ``fallback`` if the value can't round-trip. */
function cloneJSON(x, fallback = null) {
  try {
    return JSON.parse(JSON.stringify(x));
  } catch {
    return fallback;
  }
}

let incompatibleVersionWarned = false;

export function __resetIncompatibleVersionWarnedForTests() {
  incompatibleVersionWarned = false;
}

// ---------------------------------------------------------------------------
// Value-based deep equality — ORDER-INDEPENDENT for object keys. Used by the
// page's unsaved-changes indicator so a re-ordered (but structurally identical)
// object never reads as "dirty". Deliberately not a JSON.stringify compare,
// which is key-order sensitive.
// ---------------------------------------------------------------------------
export function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null) return a === b;
  if (typeof a !== 'object') return a === b;
  const aArr = Array.isArray(a);
  const bArr = Array.isArray(b);
  if (aArr !== bArr) return false;
  if (aArr) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
      if (!deepEqual(a[i], b[i])) return false;
    }
    return true;
  }
  const ak = Object.keys(a);
  const bk = Object.keys(b);
  if (ak.length !== bk.length) return false;
  for (const k of ak) {
    if (!Object.prototype.hasOwnProperty.call(b, k)) return false;
    if (!deepEqual(a[k], b[k])) return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Config sanitisation. Field-local, defensive: anything malformed collapses to
// the canonical default so a hand-edited / corrupt payload can never crash the
// page or ride back onto the wire in a broken shape.
// ---------------------------------------------------------------------------
function sanitiseForm(raw) {
  const f = isObj(raw) ? raw : {};
  const dteNum = Number(f.dte);
  return {
    start_date: typeof f.start_date === 'string' ? f.start_date : DEFAULT_FORM.start_date,
    end_date: typeof f.end_date === 'string' ? f.end_date : DEFAULT_FORM.end_date,
    // Any non-empty string is honoured (forward-compat with future expiry
    // modes exposed via /meta); a missing/blank value collapses to the default.
    expiry_mode: (typeof f.expiry_mode === 'string' && f.expiry_mode)
      ? f.expiry_mode : DEFAULT_FORM.expiry_mode,
    dte: Number.isFinite(dteNum) ? dteNum : DEFAULT_FORM.dte,
    straddle_side: (f.straddle_side === 'long' || f.straddle_side === 'short')
      ? f.straddle_side : DEFAULT_FORM.straddle_side,
    cost_enabled: Boolean(f.cost_enabled),
    cost_fallback_pts: (Number.isFinite(Number(f.cost_fallback_pts))
      && Number(f.cost_fallback_pts) >= 0)
      ? Number(f.cost_fallback_pts) : DEFAULT_FORM.cost_fallback_pts,
    // F2.2 regime-driven side knobs. Non-negative floats; a bad/missing value
    // collapses to the (inert) default so a corrupt payload never rides the wire.
    regime_side_enabled: Boolean(f.regime_side_enabled),
    regime_hvol_tolerance: (Number.isFinite(Number(f.regime_hvol_tolerance))
      && Number(f.regime_hvol_tolerance) >= 0)
      ? Number(f.regime_hvol_tolerance) : DEFAULT_FORM.regime_hvol_tolerance,
    regime_extremely_low_h20: (Number.isFinite(Number(f.regime_extremely_low_h20))
      && Number(f.regime_extremely_low_h20) >= 0)
      ? Number(f.regime_extremely_low_h20) : DEFAULT_FORM.regime_extremely_low_h20,
    regime_vvix_gate_enabled: Boolean(f.regime_vvix_gate_enabled),
    regime_vvix_gate_level: (Number.isFinite(Number(f.regime_vvix_gate_level))
      && Number(f.regime_vvix_gate_level) >= 0)
      ? Number(f.regime_vvix_gate_level) : DEFAULT_FORM.regime_vvix_gate_level,
    // F3.2 date-allowlist knobs. event_types filtered to the valid curated set;
    // dates kept as a de-duplicated array of strings. A bad/missing value
    // collapses to the (inert) default so a corrupt payload never rides the wire.
    allowlist_enabled: Boolean(f.allowlist_enabled),
    allowlist_event_types: Array.isArray(f.allowlist_event_types)
      ? [...new Set(f.allowlist_event_types.filter(
          (t) => ALLOWLIST_EVENT_TYPES.includes(t)))]
      : [...DEFAULT_FORM.allowlist_event_types],
    allowlist_dates: Array.isArray(f.allowlist_dates)
      ? [...new Set(f.allowlist_dates.filter((d) => typeof d === 'string' && d))]
      : [...DEFAULT_FORM.allowlist_dates],
  };
}

function sanitiseCustomDay(raw) {
  if (!isObj(raw)) return null;
  if (typeof raw.date !== 'string' || !raw.date) return null;
  return {
    date: raw.date,
    exclude: Boolean(raw.exclude),
    expanded: Boolean(raw.expanded),
    // Partial-override module values pass through as-is (already objects); a
    // missing override becomes null so the day inherits the global module.
    entry: isObj(raw.entry) ? raw.entry : null,
    exit: isObj(raw.exit) ? raw.exit : null,
  };
}

/**
 * Sanitise a raw config into the canonical shape the page consumes. Pure. A
 * missing / malformed module falls back to its canonical default so a load
 * always yields a runnable form.
 */
function sanitiseConfig(raw) {
  const c = isObj(raw) ? raw : {};
  const customDays = Array.isArray(c.customDays)
    ? c.customDays.map(sanitiseCustomDay).filter((x) => x !== null)
    : [];
  return {
    form: sanitiseForm(c.form),
    entry: isObj(c.entry) ? c.entry : defaultEntryModule(),
    exit: isObj(c.exit) ? c.exit : defaultExitModule(),
    hedge: isObj(c.hedge) ? c.hedge : defaultHedgeModule(),
    customDays,
  };
}

/**
 * Pure serialization boundary: page state → JSON-safe canonical config.
 * ``state`` is ``{ form, entry, exit, hedge, customDays }``. Deep-cloned so the
 * stored blob never aliases live React state, then sanitised so it is canonical
 * (idempotent with ``deserializeConfig``). Reused by the page for the
 * unsaved-changes snapshot.
 */
export function serializeConfig(state) {
  const s = isObj(state) ? state : {};
  const picked = {
    form: s.form,
    entry: s.entry,
    exit: s.exit,
    hedge: s.hedge,
    customDays: s.customDays,
  };
  return sanitiseConfig(cloneJSON(picked, {}));
}

/**
 * Pure serialization boundary: stored sim (or bare config) → canonical config
 * ready to load into page state. Accepts either a full saved-sim entry (with a
 * ``config`` field) or a bare config object.
 */
export function deserializeConfig(saved) {
  const cfg = (isObj(saved) && isObj(saved.config)) ? saved.config : saved;
  return sanitiseConfig(cloneJSON(cfg, {}));
}

function sanitiseSim(raw) {
  if (!isObj(raw)) return null;
  const id = (typeof raw.id === 'string' && raw.id) ? raw.id : null;
  if (!id) return null;
  const name = typeof raw.name === 'string' ? raw.name : '';
  const savedAt = typeof raw.savedAt === 'string' ? raw.savedAt : '';
  return { id, name, savedAt, config: sanitiseConfig(raw.config) };
}

/**
 * Migration hook. v1 is the only version so far, so this is the identity. A
 * future bump adds ``if (parsed.version === 1) parsed = migrateV1ToV2(parsed);``
 * BEFORE the version gate below, exactly like the Signals chain.
 */
function migrate(parsed) {
  return parsed;
}

// ---------------------------------------------------------------------------
// Raw read/write of the single blob. Both null-safe.
// ---------------------------------------------------------------------------
function readState() {
  const empty = { version: SCHEMA_VERSION, sims: [] };
  const ls = getStorage();
  if (!ls) return empty;
  let raw;
  try {
    raw = ls.getItem(INTRADAY_SIMS_KEY);
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
  if (!isObj(parsed)) return empty;
  parsed = migrate(parsed);
  if (parsed.version !== SCHEMA_VERSION) {
    if (!incompatibleVersionWarned) {
      incompatibleVersionWarned = true;
      // eslint-disable-next-line no-console
      console.warn(`[intraday] discarding incompatible v${parsed.version} sims`);
    }
    return empty;
  }
  const rawSims = Array.isArray(parsed.sims) ? parsed.sims : [];
  const sims = [];
  for (const s of rawSims) {
    const cleaned = sanitiseSim(s);
    if (cleaned) sims.push(cleaned);
  }
  return { version: SCHEMA_VERSION, sims };
}

function writeState(state) {
  const ls = getStorage();
  if (!ls) return;
  const sims = Array.isArray(state?.sims) ? state.sims : [];
  const payload = {
    version: SCHEMA_VERSION,
    sims: sims.map(sanitiseSim).filter((s) => s !== null),
  };
  try {
    ls.setItem(INTRADAY_SIMS_KEY, JSON.stringify(payload));
  } catch {
    // quota / sandbox — swallow; the in-memory list the caller already holds
    // is the best-effort result.
  }
}

// ---------------------------------------------------------------------------
// Public API.
// ---------------------------------------------------------------------------

/** All saved sims (sanitised), in stored order (oldest first). */
export function listSims() {
  return readState().sims;
}

/**
 * Save ``config`` under ``name`` with an IMMEDIATE, confirmed localStorage write
 * (no debounce). An existing sim with the same name is OVERWRITTEN in place
 * (its id + list position are preserved). Returns the saved entry.
 *
 * @param {string} name
 * @param {object} config  page state { form, entry, exit, hedge, customDays }
 *                         (raw state is fine — it is run through serializeConfig)
 * @returns {{id, name, savedAt, config}}
 */
export function saveSim(name, config) {
  const cleanName = (typeof name === 'string' ? name : '').trim();
  const cfg = serializeConfig(config);
  const state = readState();
  const existing = state.sims.find((s) => s.name === cleanName);
  const entry = {
    id: existing ? existing.id : newId(),
    name: cleanName,
    savedAt: new Date().toISOString(),
    config: cfg,
  };
  const sims = existing
    ? state.sims.map((s) => (s.id === existing.id ? entry : s))
    : [...state.sims, entry];
  writeState({ version: SCHEMA_VERSION, sims });
  return entry;
}

/**
 * Load a saved sim by id. Returns the sanitised entry ``{id, name, savedAt,
 * config}`` (its ``config`` is ready to spread into page state), or null when
 * no sim matches.
 */
export function loadSim(id) {
  const sim = readState().sims.find((s) => s.id === id);
  return sim || null;
}

/** Delete a saved sim by id (immediate write). Returns true if one was removed. */
export function deleteSim(id) {
  const state = readState();
  const next = state.sims.filter((s) => s.id !== id);
  if (next.length === state.sims.length) return false;
  writeState({ version: SCHEMA_VERSION, sims: next });
  return true;
}
