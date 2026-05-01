import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import IndicatorsList from './IndicatorsList';
import EditorPanel from './EditorPanel';
import ParamsPanel from './ParamsPanel';
import IndicatorChart from './IndicatorChart';
import { resolveDefaultIndexInstrument, computeIndicator } from '../../api/indicators';
import { getOptionRoots } from '../../api/options';
import { parseIndicatorSpec, reconcileParams, reconcileSeriesMap } from './paramParser';
import { DEFAULT_INDICATORS } from './defaultIndicators';
import { loadState, saveState } from './storage';
import { AUTOSAVE_KEY } from './storageKeys';
import { hydrateDefault, applyDefaultSeries } from './hydrateDefault';
import { buildPersistablePayload, serializePersistablePayload } from './persistablePayload';
import { computeDefaultSeriesBannerText } from './defaultSeriesBanner';
import {
  areAllSlotsFilled,
  computeRunDisabledReason,
  computeAssetCompatibility,
  computeOptionStreamSanity,
  deriveAssetTypeFromSeriesMap,
} from './runGate';
import SaveControls, { useAutosave } from '../../components/SaveControls';
import Card from '../../components/Card';
import ConfirmDialog from '../../components/ConfirmDialog';
import InlineNameInput from '../../components/InlineNameInput';
import useAbortableAction from '../../hooks/useAbortableAction';
import { classifyFetchError } from '../../utils/fetchError';
import { ABORTED, fetchKindToErrorType } from './errorTaxonomy';
import { normalizeErrorEnvelope } from '../../utils/errorEnvelope';
import styles from './IndicatorsPage.module.css';

const NEW_CODE_TEMPLATE = `def compute(series, window: int = 20):
    s = series['price']
    out = np.full_like(s, np.nan, dtype=float)
    out[window-1:] = np.convolve(s, np.ones(window)/window, mode='valid')
    return out`;

// Module-level cache of /api/options/roots so runIndicator doesn't re-fetch
// on every Run click. Cleared on full page reload (no invalidation otherwise
// — option roots' last_trade_date moves slowly enough that staleness within
// a session is acceptable).
let _optionRootsCache = null;
async function getOptionRootsCached() {
  if (_optionRootsCache) return _optionRootsCache;
  const resp = await getOptionRoots();
  _optionRootsCache = Array.isArray(resp?.roots) ? resp.roots : [];
  return _optionRootsCache;
}

// Default date range for option_stream computes: [last_trade_date - 6 months,
// last_trade_date] for the relevant root. Six months keeps the per-date
// materialiser fast on remote Mongo (~125 trade days × K=2 chain queries =
// ~250 round-trips) while still showing a meaningful history window.
// Falls back to [today - 6 months, today] when last_trade_date is unknown
// — the resolver surfaces per-date diagnostics if data is missing past
// the cutoff. Returns null when no option_stream refs are present.
const DEFAULT_OPTION_STREAM_LOOKBACK_MONTHS = 6;

async function deriveOptionStreamDateRange(seriesPayload) {
  const collections = new Set();
  for (const ref of Object.values(seriesPayload || {})) {
    if (ref && ref.type === 'option_stream' && ref.collection) {
      collections.add(ref.collection);
    }
  }
  if (collections.size === 0) return null;

  const roots = await getOptionRootsCached();
  // Pick the earliest last_trade_date across involved collections so a
  // multi-collection indicator (none today, but term-structure-slope
  // could grow into one) doesn't extend past the most-stale root.
  let earliest = null;
  for (const coll of collections) {
    const root = roots.find((r) => r.collection === coll);
    const ltd = root?.last_trade_date ?? null;
    if (ltd && (earliest === null || ltd < earliest)) earliest = ltd;
  }
  const end = earliest ?? isoToday();
  const start = isoMinusMonths(end, DEFAULT_OPTION_STREAM_LOOKBACK_MONTHS);
  return { start, end };
}

function isoToday() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${dd}`;
}

function isoMinusMonths(iso, months) {
  const d = new Date(`${iso}T00:00:00`);
  d.setMonth(d.getMonth() - months);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${dd}`;
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
  const [defaultSeriesLoaded, setDefaultSeriesLoaded] = useState(false);
  // Classified error from resolveDefaultIndexInstrument — drives the
  // top-banner copy. Kind ∈ 'offline' | 'network' | 'not-found' | 'server' | 'client' | 'unknown'.
  const [defaultSeriesError, setDefaultSeriesError] = useState(null);
  const [defaultAutoFilled, setDefaultAutoFilled] = useState(false);
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
  // Last payload that hit localStorage — used to derive ``dirty``.
  const [lastSavedPayload, setLastSavedPayload] = useState(null);
  // Code/Documentation tab state for the middle panel. Page-level only —
  // NOT persisted (always resets to 'code' on reload).
  const [viewMode, setViewMode] = useState('code');
  // null = confirm dialog closed; otherwise the id awaiting confirmation.
  const [pendingDeleteId, setPendingDeleteId] = useState(null);

  const indicatorsRef = useRef(indicators);
  indicatorsRef.current = indicators;

  const setAutosave = useCallback((on) => {
    setAutosaveState(on);
    try { localStorage.setItem(AUTOSAVE_KEY, String(on)); } catch { /* quota — ignore */ }
  }, []);

  // --- Hydrate on mount ------------------------------------------------
  useEffect(() => {
    const saved = loadState();
    const defaults = DEFAULT_INDICATORS.map((def) =>
      hydrateDefault(def, saved.defaultState?.[def.id]),
    );
    const userIndicators = (saved.indicators || []).map((ind) => {
      const spec = parseIndicatorSpec(ind.code || '');
      return {
        id: ind.id,
        name: ind.name,
        code: ind.code || '',
        doc: typeof ind.doc === 'string' ? ind.doc : '',
        params: reconcileParams(ind.params || {}, spec.params),
        seriesMap: reconcileSeriesMap(ind.seriesMap || {}, spec.seriesLabels),
        // Legacy payloads lacking the flag default to overlay mode.
        ownPanel: typeof ind.ownPanel === 'boolean' ? ind.ownPanel : false,
      };
    });
    const merged = [...defaults, ...userIndicators];
    setIndicators(merged);
    if (merged.length > 0) setSelectedId((curr) => curr || merged[0].id);
    // Seed lastSavedPayload with the hydrated snapshot so ``dirty``
    // stays false until the user actually mutates state.
    setLastSavedPayload(serializePersistablePayload(merged));
  }, []);

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

  // --- Autosave wiring ------------------------------------------------
  // ``currentPayload`` = the exact serialized snapshot that would be
  // persisted. ``dirty`` = this differs from what's actually on disk.
  const currentPayload = useMemo(
    () => serializePersistablePayload(indicators),
    [indicators],
  );
  const dirty = lastSavedPayload !== null && currentPayload !== lastSavedPayload;

  const commitSave = useCallback(() => {
    // Caller: autosave hook OR manual Save button.
    const payload = buildPersistablePayload(indicatorsRef.current);
    saveState(payload);
    setLastSavedPayload(serializePersistablePayload(indicatorsRef.current));
  }, []);

  useAutosave({
    enabled: autosave,
    dirty,
    value: currentPayload,
    onSave: commitSave,
    debounceMs: 500,
  });

  // --- Derived helpers -------------------------------------------------
  const selectedIndicator = useMemo(
    () => indicators.find((ind) => ind.id === selectedId) || null,
    [indicators, selectedId],
  );

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
  const handleAdd = useCallback(() => {
    setIndicators((prev) => {
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
      const newInd = {
        id,
        name: nextIndicatorName(prev),
        code: NEW_CODE_TEMPLATE,
        doc: '',
        params: reconcileParams({}, spec.params),
        seriesMap,
        ownPanel: false,
      };
      setSelectedId(id);
      return [...prev, newInd];
    });
    setError(null);
    setLastResult(null);
  }, [defaultSeries]);

  const handleDelete = useCallback((id) => {
    const target = indicatorsRef.current.find((i) => i.id === id);
    if (!target || target.readonly) return;
    setPendingDeleteId(id);
  }, []);

  const handleConfirmDelete = useCallback(() => {
    const id = pendingDeleteId;
    setPendingDeleteId(null);
    if (!id) return;
    const target = indicatorsRef.current.find((i) => i.id === id);
    if (!target || target.readonly) return;
    setIndicators((prev) => {
      const next = prev.filter((ind) => ind.id !== id);
      // If the deleted entry was selected, fall back to the first
      // remaining indicator (defaults come first in the list) so the
      // user never sees a blank middle pane when defaults are available.
      setSelectedId((sel) => {
        if (sel !== id) return sel;
        return next.length > 0 ? next[0].id : null;
      });
      return next;
    });
  }, [pendingDeleteId]);

  const handleRename = useCallback((id, newName) => {
    setIndicators((prev) => prev.map((ind) => {
      if (ind.id !== id) return ind;
      if (ind.readonly) return ind;
      return { ...ind, name: newName };
    }));
  }, []);

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
          // Legacy entries without a type field (stored before this change)
          // are treated as spot — add the type defensively.
          seriesPayload[label] = picked.type
            ? picked
            : { type: 'spot', collection: picked.collection, instrument_id: picked.instrument_id };
        }
      }
      // Option-stream materialiser walks dates per business day, so it
      // needs an explicit ISO date range. Derived from the relevant
      // root's last_trade_date (1-year lookback). Spot/continuous
      // resolvers ignore start/end so adding them is safe even when no
      // option_stream is present, but we only attach them when needed
      // to keep the request shape minimal.
      let dateRange = null;
      try {
        dateRange = await deriveOptionStreamDateRange(seriesPayload);
      } catch (e) {
        // Surface a typed error rather than swallowing — Sign 10 (no
        // silent failures). The user gets a clear message if the
        // /options/roots lookup itself fails.
        if (signal.aborted) return;
        setError({
          error_type: 'data',
          message: `Could not resolve option-stream date range: ${e?.message || e}`,
        });
        setLastResult(null);
        return;
      }

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
          { signal },
        );
        if (signal.aborted) return;
        setLastResult(data);
        setLastResultAssetType(resolvedAssetType);
        setLastResultIndicatorId(selectedIndicator.id);
      } catch (e) {
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
  }, [selectedIndicator, runAbortable]);

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
          search={search}
          onSearchChange={setSearch}
          currentAssetType={currentAssetType}
        />
      </div>
      <div className={styles.editorPanel}>
        <EditorPanel
          indicatorId={selectedIndicator?.id ?? null}
          code={selectedIndicator?.code ?? ''}
          onCodeChange={handleCodeChange}
          doc={selectedIndicator?.doc ?? ''}
          onDocChange={handleDocChange}
          readOnly={!selectedIndicator || !!selectedIndicator?.readonly}
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
