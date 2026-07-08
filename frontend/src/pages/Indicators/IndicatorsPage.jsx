import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import IndicatorsList from './IndicatorsList';
import EditorPanel from './EditorPanel';
import ParamsPanel from './ParamsPanel';
import IndicatorChart from './IndicatorChart';
import { resolveDefaultIndexInstrument, computeIndicator } from '../../api/indicators';
import { parseIndicatorSpec, reconcileParams, reconcileSeriesMap } from './paramParser';
import { DEFAULT_INDICATORS } from './defaultIndicators';
// saveState no longer called — backend is primary store for custom indicators.
// loadState still used for default-indicator per-session overlays.
// eslint-disable-next-line no-unused-vars
import { loadState, saveState } from './storage';
import { AUTOSAVE_KEY, OPTION_DATE_RANGE_KEY } from './storageKeys';
import { computeDefaultRange } from '../../components/OptionDateRangeControl';
import { hydrateDefault, applyDefaultSeries } from './hydrateDefault';
// eslint-disable-next-line no-unused-vars
import { buildPersistablePayload, serializePersistablePayload } from './persistablePayload';
import { computeDefaultSeriesBannerText } from './defaultSeriesBanner';
import {
  areAllSlotsFilled,
  computeRunDisabledReason,
  computeAssetCompatibility,
  computeOptionStreamSanity,
  deriveAssetTypeFromSeriesMap,
} from './runGate';
import SaveControls from '../../components/SaveControls';
import Card from '../../components/Card';
import ConfirmDialog from '../../components/ConfirmDialog';
import LockBanner from '../../components/LockBanner';
import InlineNameInput from '../../components/InlineNameInput';
import useAbortableAction from '../../hooks/useAbortableAction';
import useBackendAutosave from '../../hooks/useBackendAutosave';
import useEntityLock from '../../hooks/useEntityLock';
import {
  createIndicator, updateIndicator, archiveIndicator,
  setIndicatorLocked, describePersistenceError, isLockedError,
} from '../../api/persistence';
import { useIndicatorsList, useInvalidatePersistence } from '../../hooks/persistenceQueries';
import SaveStatus from '../../components/SaveStatus/SaveStatus';
import { classifyFetchError } from '../../utils/fetchError';
import { ABORTED, fetchKindToErrorType } from './errorTaxonomy';
import { normalizeErrorEnvelope } from '../../utils/errorEnvelope';
import styles from './IndicatorsPage.module.css';

// Seed for "new custom indicator". Written as a self-contained brief the user
// can paste into an AI chat to get a correct indicator on the first try: the
// header states the execution contract enforced by ``tcg/engine/indicator_exec.py``
// (the sandbox is the source of truth — keep this comment in sync with it), and
// the body is a runnable example. Exported so tests can assert its content; the
// backend ``test_new_indicator_template_*`` extracts it from this file verbatim
// and runs it through the real sandbox, so an invalid template can never ship.
//
// MUST contain no backtick (`) and no ``${`` — it lives in a JS template literal.
export const NEW_CODE_TEMPLATE = `# CUSTOM INDICATOR - paste this whole file into an AI and ask it to write compute().
# The sandbox enforces these rules; code that breaks them is rejected before it runs:
#  - Signature: def compute(series, NAME: int|float|bool = LITERAL, ...). First arg is
#    'series' (no default); every other arg MUST be int/float/bool with a literal
#    default, e.g. window: int = 20. The Parameters panel is built from this signature.
#  - series is a dict of label -> 1-D NumPy float64 array, e.g. series['close']; all
#    arrays share one length N. Return a 1-D float64 array of that same length N
#    (use np.nan for warm-up positions).
#  - Allowed: np (a CURATED numpy subset/facade, NOT the real module - no np.random /
#    np.linalg / np.fft) and the math module. Do NOT import anything (no pandas, no
#    scipy, no os) - there is nothing to import.
#  - Forbidden (line error): imports; eval/exec/compile/open/format; f-strings;
#    global/nonlocal; any name/attribute/string starting with '_'. (File/network/OS
#    access isn't a separate check - it's simply unreachable: nothing to import, and
#    open/eval/exec are rejected here.) Each run is killed after a 5-second limit.
#
# Example body: a simple moving average. Replace it with your indicator.
def compute(series, window: int = 20):
    close = series['close']
    n = close.shape[0]
    out = np.full(n, np.nan, dtype=float)
    if n >= window:
        weights = np.ones(window) / window
        out[window - 1:] = np.convolve(close, weights, mode='valid')
    return out`;

/**
 * Resolve the effective date range for an option_stream compute.
 *
 * The date-range control now exposes a plain ``{ start, end }`` window (the
 * preset buttons were removed in PR #58), so the user's explicit start/end is
 * sent to the backend as-is. The option_stream materialiser walks business
 * days across this range; spot/continuous resolvers ignore it.
 *
 * Returns null when no option_stream refs are present in `seriesPayload`
 * (so the caller omits start/end from the request entirely).
 */
function resolveOptionDateRange(seriesPayload, optionDateRange) {
  const hasOptionStream = Object.values(seriesPayload || {}).some(
    (ref) => ref && ref.type === 'option_stream' && ref.collection,
  );
  if (!hasOptionStream) return null;
  return { start: optionDateRange.start, end: optionDateRange.end };
}

/**
 * Check whether an indicator's seriesMap references at least one
 * option_stream entry. Used to conditionally show the date range control.
 */
export function hasOptionStreamRef(indicator) {
  return Object.values(indicator?.seriesMap || {}).some(
    (ref) => ref?.type === 'option_stream',
  );
}

/**
 * Load persisted option date range from localStorage, or return a default.
 *
 * The stored shape is now ``{ start, end }``. A legacy value may also carry a
 * ``preset`` key (from before PR #58 removed the preset buttons) — we simply
 * read start/end and ignore ``preset``. Any corrupt / missing / non-string
 * start|end falls back to the default 1-year window.
 */
function loadOptionDateRange() {
  try {
    const raw = localStorage.getItem(OPTION_DATE_RANGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed.start === 'string' && typeof parsed.end === 'string') {
        return { start: parsed.start, end: parsed.end };
      }
    }
  } catch {
    // Corrupt / inaccessible — use default.
  }
  return computeDefaultRange();
}

/**
 * Persist option date range to localStorage. Stores only ``{ start, end }``.
 */
function saveOptionDateRange(value) {
  try {
    localStorage.setItem(
      OPTION_DATE_RANGE_KEY,
      JSON.stringify({ start: value.start, end: value.end }),
    );
  } catch {
    // Quota — ignore.
  }
}

function nextIndicatorName(existing) {
  let maxN = 0;
  for (const ind of existing) {
    const m = /^Indicator\s+(\d+)$/i.exec(ind.name || '');
    if (m) {
      const n = parseInt(m[1], 10);
      if (!Number.isNaN(n) && n > maxN) maxN = n;
    }
  }
  return `Indicator ${maxN + 1}`;
}

/**
 * Pack a custom indicator's mutable fields into the opaque ``definition``
 * dict the backend stores. The backend treats ``definition`` as an
 * arbitrary JSON object — the shape is our convention only.
 */
function packDefinition(ind) {
  return {
    code: ind.code ?? '',
    params: ind.params ?? {},
    seriesMap: ind.seriesMap ?? {},
    doc: typeof ind.doc === 'string' ? ind.doc : '',
    ownPanel: !!ind.ownPanel,
  };
}

/**
 * Unpack a backend indicator document into the shape the page works with.
 * Mirrors the loadState → userIndicators mapping, but sources from the
 * backend ``{ id, name, definition }`` envelope instead of localStorage.
 */
function unpackBackendIndicator(doc) {
  const def = doc.definition || {};
  const code = typeof def.code === 'string' ? def.code : '';
  const spec = parseIndicatorSpec(code);
  return {
    id: doc.id,
    name: doc.name || 'Untitled',
    locked: typeof doc.locked === 'boolean' ? doc.locked : false,
    code,
    doc: typeof def.doc === 'string' ? def.doc : '',
    params: reconcileParams(def.params || {}, spec.params),
    seriesMap: reconcileSeriesMap(def.seriesMap || {}, spec.seriesLabels),
    ownPanel: typeof def.ownPanel === 'boolean' ? def.ownPanel : false,
  };
}

/**
 * Serialise the selected custom indicator's saveable fields into a stable
 * JSON string for dirty-tracking with ``useBackendAutosave``. Returns
 * ``null`` when nothing should be saved (no selection or default selected).
 */
function serializeForBackend(ind) {
  if (!ind || ind.readonly) return null;
  return JSON.stringify({ name: ind.name, definition: packDefinition(ind) });
}

// Re-export hydrateDefault for tests that import from this module.
export { hydrateDefault };

function IndicatorsPage() {
  const [indicators, setIndicators] = useState([]); // merged list (defaults + user)
  const [selectedId, setSelectedId] = useState(null);
  const [search, setSearch] = useState('');
  const { run: runAbortable, running, abort: abortRun } = useAbortableAction();
  const [error, setError] = useState(null); // structured: { error_type, message, traceback? }
  const [lastResult, setLastResult] = useState(null);
  // The asset_type the lastResult was computed against (derived from
  // the seriesMap at run time). Tracks the "pinned" run so the chart
  // panel can detect when the user later switches asset slots and the
  // pinned result is no longer compatible — see the
  // ``pinnedIncompatBanner`` block below.
  const [lastResultAssetType, setLastResultAssetType] = useState(null);
  const [lastResultIndicatorId, setLastResultIndicatorId] = useState(null);
  const [defaultSeries, setDefaultSeries] = useState(null);
  // Live progress for the option_stream materialiser. Fraction in [0, 1]
  // updated by the polling loop in ``computeIndicator``. ``null`` means
  // "no progress reported" (either not an option_stream compute, or the
  // first poll hasn't returned yet) and the UI shows a generic spinner.
  const [computeProgress, setComputeProgress] = useState(null);
  const [defaultSeriesLoaded, setDefaultSeriesLoaded] = useState(false);
  // Classified error from resolveDefaultIndexInstrument — drives the
  // top-banner copy. Kind ∈ 'offline' | 'network' | 'not-found' | 'server' | 'client' | 'unknown'.
  const [defaultSeriesError, setDefaultSeriesError] = useState(null);
  const [defaultAutoFilled, setDefaultAutoFilled] = useState(false);
  // User-configurable option date range — replaces the hardcoded 6-month
  // lookback. Persisted in localStorage via a separate key so it survives
  // across sessions without bumping the indicators schema version. Default is
  // a 1-year window ending today (the preset buttons were removed in PR #58).
  const [optionDateRange, setOptionDateRange] = useState(loadOptionDateRange);
  const handleOptionDateRangeChange = useCallback((newRange) => {
    setOptionDateRange(newRange);
    saveOptionDateRange(newRange);
  }, []);
  const [autosave, setAutosaveState] = useState(() => {
    try {
      const raw = localStorage.getItem(AUTOSAVE_KEY);
      // Default ON when unset (match prior always-autosave behaviour).
      if (raw === null) return true;
      return raw === 'true';
    } catch {
      return true;
    }
  });
  // Code/Documentation tab state for the middle panel. Page-level only —
  // NOT persisted (always resets to 'code' on reload).
  const [viewMode, setViewMode] = useState('code');
  // null = confirm dialog closed; otherwise the id awaiting confirmation.
  const [pendingDeleteId, setPendingDeleteId] = useState(null);
  // Backend save error message — shown in SaveStatus tooltip.
  const [cloudError, setCloudError] = useState(null);

  const indicatorsRef = useRef(indicators);
  indicatorsRef.current = indicators;

  // --- Indicators list: TanStack query (the persisted, user-mutable source) -
  // The custom-indicator list is now a cached query. ``indicators`` (local
  // state) remains the editable, optimistically-updated, defaults-merged copy
  // exactly as before; the query only supplies fresh server snapshots that the
  // hydration effect merges in. A mutation calls invalidate.indicators() →
  // background refetch → re-hydrate (defaults preserved; an in-progress edit
  // on the selected indicator is NOT clobbered — see the merge below).
  const indicatorsQuery = useIndicatorsList();
  const invalidate = useInvalidatePersistence();

  const setAutosave = useCallback((on) => {
    setAutosaveState(on);
    try { localStorage.setItem(AUTOSAVE_KEY, String(on)); } catch { /* quota — ignore */ }
  }, []);

  // Track the "last seen from backend" snapshot per selectedId to
  // suppress the FIRST autosave cycle after hydrate-on-mount/select.
  const lastHydratedPayloadRef = useRef({ id: null, payload: null });

  // --- Hydrate from the indicators query -------------------------------
  // Merge hardcoded defaults (always, with localStorage per-session overlays)
  // with the backend user-indicators supplied by the query. Runs on the first
  // load AND whenever a mutation-triggered invalidation lands a fresh snapshot.
  //
  // Optimistic-edit preservation: if a user indicator currently in local state
  // is unsaved-dirty (its serialized form differs from the server doc), we KEEP
  // the local copy in the merge so a background refetch never clobbers an edit
  // in progress. New docs appear; archived docs drop — both reconcile from the
  // server. This preserves the existing optimistic model exactly.
  // (1) Load DEFAULTS immediately on mount — independent of the backend query
  // so the page always shows the default indicators even if the backend is
  // unreachable (preserves the original graceful-degradation behaviour and the
  // synchronous-on-mount selection the tests rely on). Per-session overlays
  // from localStorage still apply to defaults.
  useEffect(() => {
    const saved = loadState();
    const defaults = DEFAULT_INDICATORS.map((def) =>
      hydrateDefault(def, saved.defaultState?.[def.id]),
    );
    setIndicators((prev) => {
      // Preserve any user-indicators already merged in (query may have landed
      // first); only (re)seed the readonly defaults that aren't present yet.
      const userExisting = prev.filter((ind) => !ind.readonly);
      const haveDefaults = new Set(prev.filter((i) => i.readonly).map((i) => i.id));
      const seededDefaults = defaults.map((d) =>
        haveDefaults.has(d.id) ? prev.find((p) => p.id === d.id) : d,
      );
      return [...seededDefaults, ...userExisting];
    });
    setSelectedId((curr) => curr || (defaults.length > 0 ? defaults[0].id : null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // (2) Merge BACKEND user-indicators when the query lands a snapshot. Runs on
  // the first successful load AND on every mutation-triggered invalidation.
  // Optimistic-edit preservation: a user indicator that is unsaved-dirty in
  // local state (its serialized form differs from the server doc) keeps its
  // LOCAL copy so a background refetch never clobbers an in-progress edit.
  // New docs appear; archived docs drop. Defaults are left untouched here.
  useEffect(() => {
    const backendDocs = indicatorsQuery.data;
    if (!backendDocs) return; // not yet loaded (or errored → defaults-only stands)

    const serverUser = (Array.isArray(backendDocs) ? backendDocs : [])
      .map(unpackBackendIndicator);

    setIndicators((prev) => {
      const localUserById = new Map(
        prev.filter((ind) => !ind.readonly).map((ind) => [ind.id, ind]),
      );
      const userIndicators = serverUser.map((serverInd) => {
        const local = localUserById.get(serverInd.id);
        if (!local) return serverInd; // brand-new from server
        const dirty = serializeForBackend(local) !== serializeForBackend(serverInd);
        return dirty ? local : serverInd; // keep unsaved local edits
      });
      const defaults = prev.filter((ind) => ind.readonly);
      return [...defaults, ...userIndicators];
    });

    // Select the first server indicator if nothing is selected yet (matches the
    // original behaviour where the first user indicator could become current).
    if (serverUser.length > 0) {
      setSelectedId((curr) => curr || serverUser[0].id);
      // Seed the last-hydrated ref so autosave doesn't immediately re-PUT.
      lastHydratedPayloadRef.current = {
        id: serverUser[0].id,
        payload: serializeForBackend(serverUser[0]),
      };
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indicatorsQuery.data]);

  // --- Resolve default SPX-ish instrument once -------------------------
  useEffect(() => {
    let cancelled = false;
    resolveDefaultIndexInstrument()
      .then((envelope) => {
        if (cancelled) return;
        // New envelope shape: { ok: true, data } | { ok: false, error }.
        if (envelope && envelope.ok === false) {
          setDefaultSeries(null);
          setDefaultSeriesError(envelope.error || null);
        } else {
          setDefaultSeries((envelope && envelope.data) || null);
          setDefaultSeriesError(null);
        }
        setDefaultSeriesLoaded(true);
      })
      .catch(() => {
        if (cancelled) return;
        setDefaultSeries(null);
        setDefaultSeriesError({
          kind: 'unknown',
          title: 'Unexpected error',
          message: 'Failed to resolve default series.',
        });
        setDefaultSeriesLoaded(true);
      });
    return () => { cancelled = true; };
  }, []);

  // --- Auto-fill default indicators' empty slots once SPX is known ----
  useEffect(() => {
    if (!defaultSeriesLoaded || defaultAutoFilled) return;
    setDefaultAutoFilled(true);
    if (!defaultSeries) return;
    setIndicators((prev) => prev.map((ind) =>
      ind.readonly ? applyDefaultSeries(ind, defaultSeries) : ind,
    ));
  }, [defaultSeriesLoaded, defaultSeries, defaultAutoFilled]);

  // --- Derived helpers (needed before autosave wiring) -----------------
  const selectedIndicator = useMemo(
    () => indicators.find((ind) => ind.id === selectedId) || null,
    [indicators, selectedId],
  );

  // --- Backend autosave wiring -----------------------------------------
  // Only custom (non-readonly) indicators go through the backend.
  // Default-indicator per-session overrides stay in localStorage (minor
  // UI preferences — params/seriesMap picks that don't warrant a round-trip).

  // Stable serialized payload for the SELECTED custom indicator — used
  // as the ``payload`` for ``useBackendAutosave`` so reference-identity
  // changes don't re-trigger the debounce.
  const selectedPayloadSerialized = useMemo(
    () => serializeForBackend(selectedIndicator),
    [selectedIndicator],
  );

  // backendDirty: the selected custom indicator has been edited since
  // the last hydrated snapshot. Only true for custom indicators.
  const backendDirty = !!selectedPayloadSerialized
    && (lastHydratedPayloadRef.current.id !== selectedId
        || lastHydratedPayloadRef.current.payload !== selectedPayloadSerialized);

  // Ref to the autosave hook's reset() so the locked-save handler (declared
  // before the hook) can clear a transient 'saving'/'error' status when it
  // flips the indicator to locked. Seeded just below the hook.
  const resetCloudStatusRef = useRef(() => {});

  const handleBackendSave = useCallback(async (payloadStr, { signal } = {}) => {
    if (!selectedId || !payloadStr) return;
    const body = JSON.parse(payloadStr);
    try {
      await updateIndicator(selectedId, body, { signal });
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      // 423 Locked: flip the LOCAL locked flag so the editor goes read-only
      // with the normal lock banner instead of a generic error. Defaults
      // (readonly) are never the autosave target, so no readonly guard here.
      if (isLockedError(err)) {
        setIndicators((prev) => prev.map((ind) => (ind.id !== selectedId ? ind : { ...ind, locked: true })));
        setCloudError(null);
        resetCloudStatusRef.current();
        return;
      }
      setCloudError(describePersistenceError(err));
      // eslint-disable-next-line no-console
      console.error('updateIndicator (autosave) failed:', err);
      throw err;
    }
    if (signal && signal.aborted) return;
    setCloudError(null);
    lastHydratedPayloadRef.current = { id: selectedId, payload: payloadStr };
  }, [selectedId]);

  const {
    status: cloudStatus,
    saveNow: saveNowCloud,
    reset: resetCloudStatus,
  } = useBackendAutosave({
    // Suspend autosave while the indicator is locked — the server would 423,
    // and the editor is already read-only. Mirrors SignalsPage. Without this,
    // flipping ``locked`` on a 423 (below) would re-fire the same save in a
    // loop since the lock flag is not part of the dirty-tracked payload.
    enabled: autosave && backendDirty && !(selectedIndicator && selectedIndicator.locked),
    payload: selectedPayloadSerialized,
    onSave: handleBackendSave,
  });
  resetCloudStatusRef.current = resetCloudStatus;

  // When the selection changes, reset cloud status and seed the
  // hydrated ref so the new selection doesn't trigger a spurious save.
  useEffect(() => {
    resetCloudStatus();
    if (selectedIndicator && !selectedIndicator.readonly) {
      lastHydratedPayloadRef.current = {
        id: selectedIndicator.id,
        payload: serializeForBackend(selectedIndicator),
      };
    }
    // Intentionally only depends on selectedId — we want this to fire
    // on selection change, not on every indicator mutation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, resetCloudStatus]);

  // Derive a user-facing dirty flag. For custom indicators, dirty means
  // the serialized payload differs from the last hydrated snapshot. For
  // defaults, always false (their per-session overrides are negligible).
  const dirty = backendDirty;

  // Manual Save button: persist the CURRENT payload immediately and
  // unconditionally via ``saveNow`` — works whether autosave is on or
  // off, and whether or not a debounce timer is pending. (The old
  // ``flush``-only path was a no-op when autosave was off.)
  const commitSave = useCallback(() => {
    if (!selectedId) return;
    saveNowCloud();
  }, [selectedId, saveNowCloud]);

  // --- Derived helpers -------------------------------------------------
  const parsedSpec = useMemo(
    () => parseIndicatorSpec(selectedIndicator?.code || ''),
    [selectedIndicator?.code],
  );

  const filteredIndicators = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return indicators;
    return indicators.filter((ind) => (ind.name || '').toLowerCase().includes(q));
  }, [indicators, search]);

  // --- Mutations -------------------------------------------------------
  const handleAdd = useCallback(async () => {
    const id = (globalThis.crypto && globalThis.crypto.randomUUID)
      ? globalThis.crypto.randomUUID()
      : `ind-${Date.now()}-${Math.random()}`;
    const spec = parseIndicatorSpec(NEW_CODE_TEMPLATE);
    const seriesMap = reconcileSeriesMap({}, spec.seriesLabels);
    // If we know the SPX default, pre-populate the 'price' slot.
    if (defaultSeries) {
      for (const label of Object.keys(seriesMap)) {
        if (seriesMap[label] === null) {
          seriesMap[label] = {
            type: 'spot',
            collection: defaultSeries.collection,
            instrument_id: defaultSeries.instrument_id,
          };
        }
      }
    }
    const name = nextIndicatorName(indicatorsRef.current);
    const newInd = {
      id,
      name,
      code: NEW_CODE_TEMPLATE,
      doc: '',
      params: reconcileParams({}, spec.params),
      seriesMap,
      ownPanel: false,
    };

    // Optimistically add to local state, then persist to backend.
    setIndicators((prev) => [...prev, newInd]);
    setSelectedId(id);
    setError(null);
    setLastResult(null);

    // Seed hydrated ref so autosave doesn't immediately re-save.
    lastHydratedPayloadRef.current = {
      id,
      payload: serializeForBackend(newInd),
    };

    try {
      await createIndicator({
        id,
        name,
        definition: packDefinition(newInd),
      });
      // Refresh the list from the server by invalidating the indicators query
      // → background refetch → re-hydrate (the merge preserves this indicator's
      // local copy if the user has already started editing it).
      invalidate.indicators(id);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('createIndicator failed:', err);
      // Roll back — remove the optimistically added indicator.
      setIndicators((prev) => prev.filter((ind) => ind.id !== id));
      setSelectedId((sel) => sel === id ? null : sel);
      setCloudError(describePersistenceError(err));
    }
  }, [defaultSeries, invalidate]);

  const handleDelete = useCallback((id) => {
    const target = indicatorsRef.current.find((i) => i.id === id);
    if (!target || target.readonly) return;
    setPendingDeleteId(id);
  }, []);

  const handleConfirmDelete = useCallback(async () => {
    const id = pendingDeleteId;
    setPendingDeleteId(null);
    if (!id) return;
    const target = indicatorsRef.current.find((i) => i.id === id);
    if (!target || target.readonly) return;

    // Optimistically remove from local state.
    setIndicators((prev) => {
      const next = prev.filter((ind) => ind.id !== id);
      setSelectedId((sel) => {
        if (sel !== id) return sel;
        return next.length > 0 ? next[0].id : null;
      });
      return next;
    });

    try {
      await archiveIndicator(id);
      // Sync with server truth (the archived doc drops from the list).
      invalidate.indicators(id);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('archiveIndicator failed:', err);
      // Roll back — re-add the indicator.
      setIndicators((prev) => [...prev, target]);
      setCloudError(describePersistenceError(err));
    }
  }, [pendingDeleteId, invalidate]);

  const handleRename = useCallback((id, newName) => {
    setIndicators((prev) => prev.map((ind) => {
      if (ind.id !== id) return ind;
      if (ind.readonly) return ind;
      return { ...ind, name: newName };
    }));
  }, []);

  // Shared lock-handler hook (same shape across all three pages). Indicators
  // uses an OPTIMISTIC flip + rollback; readonly defaults never carry a lock
  // toggle, so the readonly guard here is purely defensive.
  const applyIndicatorLocked = useCallback((id, lockedVal) => {
    setIndicators((prev) => prev.map((ind) => {
      if (ind.id !== id) return ind;
      if (ind.readonly) return ind;
      return { ...ind, locked: lockedVal };
    }));
  }, []);
  const handleSetIndicatorLocked = useEntityLock({
    // Lazy wrapper — defers the api import access to call time so test
    // mocks that omit setIndicatorLocked don't trip a render-time getter.
    setLocked: useCallback((id, next) => setIndicatorLocked(id, next), []),
    applyLocked: applyIndicatorLocked,
    optimistic: true,
    onSuccess: useCallback((doc) => {
      // applyLocked already patched the lock flag from the server doc; invalidate
      // to keep the cached list coherent (refetch returns equal data → the merge
      // takes the server copy with no visible change).
      if (doc && doc.id) invalidate.indicators(doc.id);
    }, [invalidate]),
    onError: useCallback((err) => {
      setCloudError(describePersistenceError(err));
      // eslint-disable-next-line no-console
      console.error('setIndicatorLocked failed:', err);
    }, []),
  });

  const handleCodeChange = useCallback((code) => {
    setIndicators((prev) => prev.map((ind) => {
      if (ind.id !== selectedId) return ind;
      if (ind.readonly) return ind; // defensive — CodeEditor also blocks this
      const spec = parseIndicatorSpec(code);
      const nextParams = reconcileParams(ind.params, spec.params);
      const nextSeriesMap = reconcileSeriesMap(ind.seriesMap, spec.seriesLabels);
      return { ...ind, code, params: nextParams, seriesMap: nextSeriesMap };
    }));
  }, [selectedId]);

  const handleDocChange = useCallback((doc) => {
    setIndicators((prev) => prev.map((ind) => {
      if (ind.id !== selectedId) return ind;
      if (ind.readonly) return ind; // defensive — DocView also blocks this
      // No spec reparse: ``doc`` is plain markdown, it cannot affect
      // params or series labels. The dirty flag picks up the change via
      // serializePersistablePayload (which now includes ``doc``).
      return { ...ind, doc: typeof doc === 'string' ? doc : '' };
    }));
  }, [selectedId]);

  const handleOwnPanelChange = useCallback((on) => {
    setIndicators((prev) => prev.map((ind) => {
      if (ind.id !== selectedId) return ind;
      // Defaults are locked — defend in depth in case the UI ever sends
      // an event for one (the checkbox should already be disabled).
      if (ind.readonly) return ind;
      return { ...ind, ownPanel: !!on };
    }));
  }, [selectedId]);

  const handleParamChange = useCallback((name, value) => {
    setIndicators((prev) => prev.map((ind) => {
      if (ind.id !== selectedId) return ind;
      return { ...ind, params: { ...ind.params, [name]: value } };
    }));
  }, [selectedId]);

  const handleSeriesSave = useCallback((label, entry) => {
    setIndicators((prev) => prev.map((ind) => {
      if (ind.id !== selectedId) return ind;
      // Persist the full SeriesRef discriminated union (type + all fields).
      // Spot:       { type: 'spot', collection, instrument_id }
      // Continuous: { type: 'continuous', collection, adjustment, cycle, rollOffset, strategy }
      return {
        ...ind,
        seriesMap: {
          ...ind.seriesMap,
          [label]: entry,
        },
      };
    }));
  }, [selectedId]);

  const runIndicator = useCallback(async () => {
    if (!selectedIndicator) return;
    setError(null);

    // Derive the run-time asset_type from the filled seriesMap. We
    // surface a structured error (Sign 10 — never silently pick one)
    // when slots disagree, and forward the resolved type + the
    // indicator's compat declaration to the backend so it can do its
    // own canonical cross-check.
    const derived = deriveAssetTypeFromSeriesMap(selectedIndicator.seriesMap);
    if (!derived.ok) {
      setError({
        error_type: 'validation',
        message: `Series slots disagree on asset type (${derived.types.join(', ')}). Pick consistent series before running.`,
      });
      setLastResult(null);
      return;
    }
    const resolvedAssetType = derived.asset_type;

    // Pre-flight compat check — refuse to even fire the request when
    // the indicator declares a compat list and the resolved type is
    // not in it. Mirrors the backend's 422; doing it here saves a
    // round-trip and keeps the failure typed.
    const compat = computeAssetCompatibility(selectedIndicator);
    if (!compat.ok && compat.reason === 'incompatible_asset') {
      setError({
        error_type: 'incompatible_asset',
        error_code: 'INDICATOR_INCOMPATIBLE_ASSET',
        message: `Requires ${compat.accepted_asset_types.join(' or ')} data; current asset is ${compat.asset_type}.`,
        accepted_asset_types: compat.accepted_asset_types,
        asset_type: compat.asset_type,
        indicator_id: selectedIndicator.id,
      });
      setLastResult(null);
      return;
    }

    await runAbortable(async ({ signal }) => {
      const seriesPayload = {};
      for (const [label, picked] of Object.entries(selectedIndicator.seriesMap || {})) {
        if (picked) {
          // Send the full SeriesRef discriminated union. The backend
          // /api/indicators/compute accepts:
          //   { type: 'spot', collection, instrument_id }
          //   { type: 'continuous', collection, adjustment, cycle, rollOffset, strategy }
          //   { type: 'option_stream', collection, option_type, cycle,
          //     maturity, selection, stream, roll_offset }
          // The picked ref is forwarded verbatim, so option_stream's roll_offset
          // (when set) rides along unchanged. Option streams carry no
          // back-adjustment, so there is no adjustment field on that variant.
          // Legacy entries without a type field (stored before this change)
          // are treated as spot — add the type defensively.
          seriesPayload[label] = picked.type
            ? picked
            : { type: 'spot', collection: picked.collection, instrument_id: picked.instrument_id };
        }
      }
      // Option-stream materialiser walks dates per business day, so it
      // needs an explicit ISO date range. We forward the user's configured
      // OptionDateRangeControl window verbatim ({ start, end }); spot/
      // continuous resolvers ignore start/end so attaching them is safe, but
      // we only do so when an option_stream ref is present to keep the
      // request shape minimal. ``resolveOptionDateRange`` is a pure, local,
      // synchronous selector (no network call) — it returns null when no
      // option_stream ref is present, so the caller omits start/end entirely.
      const dateRange = resolveOptionDateRange(seriesPayload, optionDateRange);

      // Reset any prior progress so the spinner starts fresh; the
      // poll loop will update it the moment the backend reports.
      setComputeProgress(dateRange ? 0 : null);
      try {
        const data = await computeIndicator(
          {
            code: selectedIndicator.code,
            params: selectedIndicator.params,
            series: seriesPayload,
            // Forward both fields when known. The backend treats them
            // as the canonical source for the compat check; an empty
            // string / undefined means "don't check".
            ...(resolvedAssetType ? { asset_type: resolvedAssetType } : {}),
            ...(Array.isArray(selectedIndicator.compatibleAssetTypes)
              ? { compatible_asset_types: selectedIndicator.compatibleAssetTypes }
              : {}),
            ...(dateRange ? { start: dateRange.start, end: dateRange.end } : {}),
          },
          {
            signal,
            // Only forward the callback when there's actually a slow
            // path to track; ``computeIndicator`` only triggers polling
            // when ``onProgress`` is supplied.
            ...(dateRange
              ? { onProgress: (frac) => setComputeProgress(frac) }
              : {}),
          },
        );
        if (signal.aborted) return;
        setLastResult(data);
        setLastResultAssetType(resolvedAssetType);
        setLastResultIndicatorId(selectedIndicator.id);
        setComputeProgress(null);
      } catch (e) {
        // Always release the progress spinner — every catch path
        // either renders an error card or silently aborts; in both
        // cases the live "computing X%" line should disappear.
        setComputeProgress(null);
        if (signal.aborted) return;
        if (e && typeof e === 'object' && 'status' in e) {
          // Structured error envelope:
          //   { error_type: 'validation'|'runtime'|'data', message, traceback? }
          // The 422 INDICATOR_INCOMPATIBLE_ASSET case is recognised
          // via ``error_code`` in ``normalizeErrorEnvelope`` and
          // surfaces with error_type='incompatible_asset'.
          setError(normalizeErrorEnvelope(e.body, e.message || 'Request failed'));
          setLastResult(null);
        } else {
          // Classify so offline/network surfaces an accurate heading in the
          // error card rather than the misleading "Data error" label.
          const classified = classifyFetchError(e);
          const error_type = fetchKindToErrorType(classified.kind);
          if (error_type === ABORTED) {
            // Silently suppress cancelled requests — don't render an error card.
            setLastResult(null);
          } else {
            setError({
              error_type,
              message: `${classified.title} — ${classified.message}`,
            });
            setLastResult(null);
          }
        }
      }
    });
  }, [selectedIndicator, runAbortable, optionDateRange]);

  // Detach the pinned result — clears the lastResult for the current
  // indicator (used by the pinned-meets-incompat banner's Detach
  // button). The result is NOT auto-cleared; the user must explicitly
  // dismiss it (no silent removal).
  const detachPinnedResult = useCallback(() => {
    setLastResult(null);
    setLastResultAssetType(null);
    setLastResultIndicatorId(null);
    setError(null);
  }, []);

  // Cancel any in-flight run when the user switches indicators —
  // otherwise a stale response could overwrite state for the new one.
  useEffect(() => {
    return () => abortRun();
  }, [selectedId, abortRun]);

  const seriesLabels = parsedSpec.seriesLabels;
  const allSlotsFilled = areAllSlotsFilled(selectedIndicator, seriesLabels);

  // Pre-flight option_stream sanity (tautological by_delta+stream=delta).
  // Mirrors the asset-type compat check — refuses the request before
  // firing rather than letting the backend reject deterministically.
  const streamSanity = computeOptionStreamSanity(selectedIndicator);

  // Show date range control when the selected indicator has option_stream refs.
  const showDateRange = hasOptionStreamRef(selectedIndicator);

  const canRun = !!selectedIndicator
    && !running
    && allSlotsFilled
    && !!(selectedIndicator.code && selectedIndicator.code.trim())
    && streamSanity.ok;

  // Tooltip shown on the disabled Run button so keyboard and mouse users
  // can tell what's blocking execution. Priority: most-specific first.
  const runDisabledReason = canRun || running
    ? null
    : computeRunDisabledReason(selectedIndicator, seriesLabels);

  // Banner copy driven by the classified resolver result. If we never
  // got a classified error (just no match), fall back to the original
  // "pick a series manually" message.
  const bannerText = computeDefaultSeriesBannerText({
    defaultSeriesLoaded,
    defaultSeries,
    defaultSeriesError,
  });

  // Currently-selected asset type, derived from the SELECTED
  // indicator's seriesMap. Drives the picker grey-out and the
  // pinned-meets-incompat banner. ``null`` is meaningful: it means
  // "we cannot classify the slot yet" → picker shows everything.
  const currentAssetType = useMemo(() => {
    const derived = deriveAssetTypeFromSeriesMap(selectedIndicator?.seriesMap);
    if (!derived.ok) return null; // slot conflict → don't grey
    return derived.asset_type;
  }, [selectedIndicator?.seriesMap]);

  // Pinned-meets-incompat: the lastResult was computed for an
  // asset_type that is no longer in the indicator's compat list (e.g.
  // user changed a series slot to a different asset). The chart panel
  // renders a banner instead of the chart; the banner offers a
  // ``Detach`` button that clears lastResult.
  const pinnedIncompat = useMemo(() => {
    if (!lastResult || !selectedIndicator) return null;
    if (lastResultIndicatorId !== selectedIndicator.id) return null;
    const compat = selectedIndicator.compatibleAssetTypes;
    if (!Array.isArray(compat) || compat.length === 0) return null;
    if (!lastResultAssetType) return null;
    if (compat.includes(lastResultAssetType)) return null;
    return {
      indicatorName: selectedIndicator.name || 'Indicator',
      asset_type: lastResultAssetType,
      accepted_asset_types: compat.slice(),
    };
  }, [lastResult, selectedIndicator, lastResultIndicatorId, lastResultAssetType]);

  return (
    <div className={`${styles.page} ${selectedIndicator?.ownPanel ? styles.pageSplit : ''}`}>
      {bannerText && (
        <div className={styles.banner} data-banner-kind={defaultSeriesError?.kind || 'not-found'}>
          {bannerText}
        </div>
      )}
      <div className={styles.listPanel}>
        <IndicatorsList
          indicators={filteredIndicators}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onAdd={handleAdd}
          onDelete={handleDelete}
          onRename={handleRename}
          onSetIndicatorLocked={handleSetIndicatorLocked}
          search={search}
          onSearchChange={setSearch}
          currentAssetType={currentAssetType}
        />
      </div>
      <div className={styles.editorPanel}>
        {selectedIndicator && !selectedIndicator.readonly && selectedIndicator.locked && (
          <LockBanner
            entityLabel="indicator"
            className={styles.lockBanner}
            testId="editor-lock-banner"
          />
        )}
        <EditorPanel
          indicatorId={selectedIndicator?.id ?? null}
          code={selectedIndicator?.code ?? ''}
          onCodeChange={handleCodeChange}
          doc={selectedIndicator?.doc ?? ''}
          onDocChange={handleDocChange}
          readOnly={!selectedIndicator || !!selectedIndicator?.readonly || !!selectedIndicator?.locked}
          viewMode={viewMode}
          onViewModeChange={setViewMode}
        />
      </div>
      <div className={styles.paramsPanel}>
        <div className={styles.paramsTopBar}>
          <SaveControls
            className={styles.paramsSaveControls}
            dirty={dirty}
            autosave={autosave}
            onSave={commitSave}
            onToggleAutosave={setAutosave}
            saveDisabled={!!(selectedIndicator && !selectedIndicator.readonly && selectedIndicator.locked)}
            leftSlot={
              <InlineNameInput
                entity={selectedIndicator}
                onRename={handleRename}
                className={styles.nameInput}
                placeholder="Select an indicator"
                selectedPlaceholder="Indicator name"
                ariaLabel="Indicator name"
                title={(ent) => (
                  !ent || ent.readonly
                    ? 'Default indicator — name is fixed'
                    : 'Indicator name'
                )}
              />
            }
          />
          <SaveStatus
            status={cloudStatus}
            errorMessage={cloudError}
          />
        </div>
        <ParamsPanel
          indicator={selectedIndicator}
          paramsSpec={parsedSpec.params}
          seriesLabels={parsedSpec.seriesLabels}
          onParamChange={handleParamChange}
          onSeriesSave={handleSeriesSave}
          onRun={runIndicator}
          running={running}
          canRun={canRun}
          runDisabledReason={runDisabledReason}
          defaultCollection={defaultSeries?.collection || null}
          ownPanel={!!selectedIndicator?.ownPanel}
          onOwnPanelChange={handleOwnPanelChange}
          showDateRange={showDateRange}
          optionDateRange={optionDateRange}
          onOptionDateRangeChange={handleOptionDateRangeChange}
          readOnly={!!selectedIndicator?.locked}
        />
      </div>
      <div className={styles.chartPanel}>
        <Card
          title="Results"
          className={styles.resultsCard}
          bodyClassName={styles.resultsCardBody}
          data-testid="results-card"
        >
          <IndicatorChart
            indicator={selectedIndicator}
            result={lastResult}
            loading={running}
            loadingProgress={running ? computeProgress : null}
            error={error}
            pinnedIncompat={pinnedIncompat}
            onDetachPinned={detachPinnedResult}
          />
        </Card>
      </div>
      <ConfirmDialog
        open={pendingDeleteId !== null}
        title="Delete indicator?"
        message="This indicator will be permanently removed from your library."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        destructive
        onConfirm={handleConfirmDelete}
        onCancel={() => setPendingDeleteId(null)}
      />
    </div>
  );
}

export default IndicatorsPage;
