import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import IndicatorsList from './IndicatorsList';
import EditorPanel from './EditorPanel';
import ParamsPanel from './ParamsPanel';
import IndicatorChart from './IndicatorChart';
import { resolveDefaultIndexInstrument, computeIndicator } from '../../api/indicators';
import { parseIndicatorSpec, reconcileParams, reconcileSeriesMap } from './paramParser';
import { DEFAULT_INDICATORS } from './defaultIndicators';
import { loadState, saveState } from './storage';
import { AUTOSAVE_KEY } from './storageKeys';
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

// Hydrate a default indicator from the registry + persisted per-session
// state. Returns the merged shape the rest of the page works with.
//
// Exported for unit tests. ``chartMode`` is a registry-only author hint
// (no user-editable counterpart in localStorage) — it flows straight
// from ``def`` into the hydrated object and is NEVER overridden by the
// ``defaultState`` overlay, which only carries ``params`` / ``seriesMap``.
export function hydrateDefault(def, savedEntry) {
  const spec = parseIndicatorSpec(def.code);
  const params = reconcileParams(savedEntry?.params || {}, spec.params);
  const seriesMap = reconcileSeriesMap(savedEntry?.seriesMap || {}, spec.seriesLabels);
  const hydrated = {
    id: def.id,
    name: def.name,
    code: def.code,
    doc: typeof def.doc === 'string' ? def.doc : '',
    readonly: true,
    params,
    seriesMap,
    // ownPanel is locked at the registry — users cannot override it for defaults.
    ownPanel: !!def.ownPanel,
  };
  // chartMode is optional — only propagate when the registry entry sets
  // it, so hydrated objects for entries without the hint stay clean
  // (chart falls back to 'lines' via ``IndicatorChart.jsx``).
  if (typeof def.chartMode === 'string' && def.chartMode) {
    hydrated.chartMode = def.chartMode;
  }
  return hydrated;
}

// Build the storage-shaped payload (same shape the old persistence
// effect wrote). Pure — no side-effects.
function buildPersistablePayload(indicators) {
  const userIndicators = indicators
    .filter((ind) => !ind.readonly)
    .map((ind) => ({
      id: ind.id,
      name: ind.name,
      code: ind.code,
      doc: typeof ind.doc === 'string' ? ind.doc : '',
      params: ind.params,
      seriesMap: ind.seriesMap,
      // ``ownPanel`` is persisted for customs only — defaults source it
      // from the registry (see ``hydrateDefault``), so we intentionally
      // do NOT include it in ``defaultState`` below.
      ownPanel: !!ind.ownPanel,
    }));
  const defaultState = {};
  for (const ind of indicators) {
    if (!ind.readonly) continue;
    defaultState[ind.id] = { params: ind.params, seriesMap: ind.seriesMap };
  }
  return { indicators: userIndicators, defaultState };
}

// Stable-ish serialization for dirty comparison. JSON.stringify of a
// plain object built from sorted entries is stable across re-renders
// so long as the underlying data is the same.
function serializePersistablePayload(indicators) {
  return JSON.stringify(buildPersistablePayload(indicators));
}

// Auto-populate a default's SPX slot once the resolver returns, but
// only if the slot is still empty (user may already have picked).
function applyDefaultSeries(ind, defaultSeries) {
  if (!defaultSeries) return ind;
  const updated = { ...ind.seriesMap };
  let touched = false;
  for (const [label, picked] of Object.entries(updated)) {
    if (picked === null) {
      updated[label] = {
        type: 'spot',
        collection: defaultSeries.collection,
        instrument_id: defaultSeries.instrument_id,
      };
      touched = true;
    }
  }
  if (!touched) return ind;
  return { ...ind, seriesMap: updated };
}

function IndicatorsPage() {
  const [indicators, setIndicators] = useState([]); // merged list (defaults + user)
  const [selectedId, setSelectedId] = useState(null);
  const [search, setSearch] = useState('');
  const { run: runAbortable, running, abort: abortRun } = useAbortableAction();
  const [error, setError] = useState(null); // structured: { error_type, message, traceback? }
  const [lastResult, setLastResult] = useState(null);
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
  // iter-4: replaced window.confirm with shared ConfirmDialog.
  // pendingDeleteId holds the indicator id awaiting confirmation (null = closed).
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
    // iter-4: open shared ConfirmDialog instead of synchronous window.confirm.
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
      try {
        const data = await computeIndicator(
          {
            code: selectedIndicator.code,
            params: selectedIndicator.params,
            series: seriesPayload,
          },
          { signal },
        );
        if (signal.aborted) return;
        setLastResult(data);
      } catch (e) {
        if (signal.aborted) return;
        if (e && typeof e === 'object' && 'status' in e) {
          // Structured error envelope:
          //   { error_type: 'validation'|'runtime'|'data', message, traceback? }
          // Legacy shapes ({detail: "..."} or {message: "..."}) fall back to
          // error_type='validation'.
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

  // Cancel any in-flight run when the user switches indicators —
  // otherwise a stale response could overwrite state for the new one.
  useEffect(() => {
    return () => abortRun();
  }, [selectedId, abortRun]);

  const seriesLabels = parsedSpec.seriesLabels;
  const allSlotsFilled = selectedIndicator
    && seriesLabels.length > 0
    && seriesLabels.every((lbl) => {
      const picked = selectedIndicator.seriesMap?.[lbl];
      if (!picked || !picked.collection) return false;
      // Continuous series are identified by collection alone — no instrument_id.
      if (picked.type === 'continuous') return true;
      // Spot (and legacy entries without a type field) require instrument_id.
      return !!picked.instrument_id;
    });

  const canRun = !!selectedIndicator
    && !running
    && allSlotsFilled
    && !!(selectedIndicator.code && selectedIndicator.code.trim());

  // Tooltip shown on the disabled Run button so keyboard and mouse users
  // can tell what's blocking execution. Priority: most-specific first.
  const runDisabledReason = canRun || running ? null : (() => {
    if (!selectedIndicator) return 'Select an indicator first';
    if (!selectedIndicator.code || !selectedIndicator.code.trim()) return 'Add code before running';
    const emptyLabel = seriesLabels.find((lbl) => {
      const picked = selectedIndicator.seriesMap?.[lbl];
      if (!picked || !picked.collection) return true;
      if (picked.type === 'continuous') return false;
      return !picked.instrument_id;
    });
    if (emptyLabel) return `Fill series slot: ${emptyLabel}`;
    return 'Cannot run';
  })();

  // Banner copy driven by the classified resolver result. If we never
  // got a classified error (just no match), fall back to the original
  // "pick a series manually" message.
  const bannerText = (() => {
    if (!defaultSeriesLoaded) return null;
    if (defaultSeries) return null;
    if (defaultSeriesError) {
      const k = defaultSeriesError.kind;
      if (k === 'offline') return "You're offline — series list unavailable";
      if (k === 'network') return "Can't reach the data server";
      if (k === 'server' || k === 'client') {
        return `Data server error: ${defaultSeriesError.message || 'unknown'}`;
      }
      // 'not-found' / 'unknown' → fall through to classic copy.
    }
    return 'S\u0026P 500 not found in DB — pick a series manually.';
  })();

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
        {/*
          Iter-8: the indicator's name input + Save + Auto save now live
          at the TOP of the params (right) column, just above the
          Parameters section. The editor panel no longer carries a
          header. SaveControls still reuses its ``leftSlot`` prop — the
          Portfolio call site is untouched because ``leftSlot`` defaults
          to undefined there.
        */}
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
          />
        </Card>
      </div>
      {/* iter-4: shared ConfirmDialog replaces the previous window.confirm. */}
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
