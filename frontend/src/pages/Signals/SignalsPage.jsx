import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import SignalsList from './SignalsList';
import BlockEditor from './BlockEditor';
import ParamsPanel from './ParamsPanel';
import ResultsView from './ResultsView';
import Statistics from '../../components/Statistics';
import TradeLog from '../../components/TradeLog';
import { buildSignalStatsInputs } from './signalStatsInputs';
import ConfirmDialog from '../../components/ConfirmDialog';
import InputsPanel from './InputsPanel';
import { loadState, saveState, emptyRules, defaultSettings } from './storage';
import { AUTOSAVE_KEY } from './storageKeys';
import { computeSignal } from '../../api/signals';
import { listSignals, createSignal, updateSignal, archiveSignal } from '../../api/persistence';
import { buildComputeRequestBody } from './requestBuilder';
import { computeRunGate } from './runGate';
import { countOwnPanelIndicators } from './resultsPlotTraces';
import { classifyFetchError } from '../../utils/fetchError';
import { fetchKindToErrorType, ABORTED } from '../Indicators/errorTaxonomy';
import { normalizeErrorEnvelope } from '../../utils/errorEnvelope';
import { hydrateAvailableIndicators } from './hydrateIndicators';
import { getRiskFreeRateFraction } from '../../lib/userSettings';
import SaveControls, { useAutosave } from '../../components/SaveControls';
import SaveStatus from '../../components/SaveStatus/SaveStatus';
import useBackendAutosave from '../../hooks/useBackendAutosave';
import Card from '../../components/Card';
import InlineNameInput from '../../components/InlineNameInput';
import useAbortableAction from '../../hooks/useAbortableAction';
import styles from './SignalsPage.module.css';

function nextSignalName(existing) {
  let maxN = 0;
  for (const s of existing) {
    const m = /^Signal\s+(\d+)$/i.exec(s.name || '');
    if (m) {
      const n = parseInt(m[1], 10);
      if (!Number.isNaN(n) && n > maxN) maxN = n;
    }
  }
  return `Signal ${maxN + 1}`;
}

// Stable serialisation for dirty comparison — JSON.stringify over the
// exact shape we'd persist.
function serializePersistablePayload(signals) {
  return JSON.stringify({ signals });
}

// Build the editor-shape signal object from a backend SignalOut payload.
// Backend field ``description`` maps to local ``doc``; the rest mirror.
function hydrateFromPersisted(persisted) {
  const inputs = Array.isArray(persisted.inputs) ? persisted.inputs : [];
  const rules = (persisted.rules && typeof persisted.rules === 'object')
    ? { ...emptyRules(), ...persisted.rules }
    : emptyRules();
  const settings = (persisted.settings && typeof persisted.settings === 'object')
    ? { ...defaultSettings(), ...persisted.settings }
    : defaultSettings();
  return {
    id: persisted.id,
    name: persisted.name || 'Untitled',
    inputs,
    rules,
    settings,
    doc: typeof persisted.description === 'string' ? persisted.description : '',
  };
}

// Re-export for consumers that import from this file.
export { hydrateAvailableIndicators } from './hydrateIndicators';

function SignalsPage() {
  const [signals, setSignals] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [search, setSearch] = useState('');
  const { run: runAbortable, running, abort: abortRun } = useAbortableAction();
  const [error, setError] = useState(null);
  const [lastResult, setLastResult] = useState(null);
  const [capital, setCapital] = useState(1000);
  const [availableIndicators, setAvailableIndicators] = useState([]);
  const [autosave, setAutosaveState] = useState(() => {
    try {
      const raw = localStorage.getItem(AUTOSAVE_KEY);
      if (raw === null) return true;
      return raw === 'true';
    } catch {
      return true;
    }
  });
  const [lastSavedPayload, setLastSavedPayload] = useState(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);

  // --- Persistence state ---------------------------------------------------
  // persistedSignals: list fetched from backend for the current category.
  // They drive what is shown in the SignalsList panel.
  // The local ``signals`` array still holds full editor state. When the
  // user selects a persisted signal that isn't in local state yet, we
  // hydrate from the backend doc rather than injecting a blank skeleton —
  // this is the load-bearing fix for the "rules don't persist" bug.
  const [persistedCategory, setPersistedCategory] = useState('RESEARCH');
  const [persistedSignals, setPersistedSignals] = useState([]);
  const [persistedLoading, setPersistedLoading] = useState(false);

  const signalsRef = useRef(signals);
  signalsRef.current = signals;
  const persistedSignalsRef = useRef(persistedSignals);
  persistedSignalsRef.current = persistedSignals;

  // Separate status state for one-shot operations (add / archive /
  // category-change). Kept separate from the debounced autosave status so
  // neither path's timing can overwrite the other.
  const [oneshotStatus, setOneshotStatus] = useState('idle');

  const setAutosave = useCallback((on) => {
    setAutosaveState(on);
    try { localStorage.setItem(AUTOSAVE_KEY, String(on)); } catch { /* ignore */ }
  }, []);

  // --- Hydrate on mount ----------------------------------------------------
  useEffect(() => {
    const saved = loadState();
    const initial = (saved.signals || []).map((s) => ({
      id: s.id,
      name: s.name || 'Untitled',
      inputs: Array.isArray(s.inputs) ? s.inputs : [],
      rules: { ...emptyRules(), ...(s.rules || {}) },
      // v4 bullet #7: preserve stored dont_repeat verbatim. Sanitiser
      // already defaulted missing settings to {dont_repeat:true} at load
      // time. We do NOT override the stored value on hydrate.
      settings: (s.settings && typeof s.settings === 'object')
        ? { ...defaultSettings(), ...s.settings }
        : defaultSettings(),
      doc: typeof s.doc === 'string' ? s.doc : '',
    }));
    setSignals(initial);
    if (initial.length > 0) setSelectedId((curr) => curr || initial[0].id);
    setLastSavedPayload(serializePersistablePayload(initial));
    setAvailableIndicators(hydrateAvailableIndicators());
  }, []);

  // Re-hydrate available indicators whenever the window regains focus —
  // catches edits made on the Indicators page without a reload.
  useEffect(() => {
    function refresh() { setAvailableIndicators(hydrateAvailableIndicators()); }
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, []);

  // --- Fetch persisted signals when category changes -----------------------
  const fetchPersistedSignals = useCallback(async (cat) => {
    setPersistedLoading(true);
    try {
      const docs = await listSignals(cat);
      setPersistedSignals(docs);
    } catch {
      // Non-fatal — show empty list. Backend may be starting up.
      setPersistedSignals([]);
    } finally {
      setPersistedLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchPersistedSignals(persistedCategory);
  }, [persistedCategory, fetchPersistedSignals]);

  const currentPayload = useMemo(() => serializePersistablePayload(signals), [signals]);
  const dirty = lastSavedPayload !== null && currentPayload !== lastSavedPayload;

  const commitSave = useCallback(() => {
    saveState({ signals: signalsRef.current });
    setLastSavedPayload(serializePersistablePayload(signalsRef.current));
  }, []);

  useAutosave({
    enabled: autosave,
    dirty,
    value: currentPayload,
    onSave: commitSave,
    debounceMs: 500,
  });

  const selectedSignal = useMemo(
    () => signals.find((s) => s.id === selectedId) || null,
    [signals, selectedId],
  );

  // Is the selected signal one of the persisted ones? Only persisted
  // signals get the backend autosave treatment — un-persisted local
  // skeletons (the brief moment before a create resolves) do not.
  const selectedPersisted = useMemo(
    () => (selectedId
      ? persistedSignals.find((p) => p.id === selectedId) || null
      : null),
    [selectedId, persistedSignals],
  );

  // The SignalsList displays backend-persisted signals, filtered by name search.
  // persistedSignals provides the authoritative list for the current category.
  const filteredPersistedSignals = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return persistedSignals;
    return persistedSignals.filter((s) => (s.name || '').toLowerCase().includes(q));
  }, [persistedSignals, search]);

  // When a persisted signal is selected that has no local editor state
  // yet, hydrate from the backend doc — NOT a blank skeleton. This is
  // the load-bearing fix for the "rules don't survive a refresh" bug.
  const handleSelectPersisted = useCallback((id) => {
    setSelectedId(id);
    setSignals((prev) => {
      const existing = prev.find((s) => s.id === id);
      const persisted = persistedSignalsRef.current.find((p) => p.id === id);
      if (!persisted) return prev;
      const hydrated = hydrateFromPersisted(persisted);
      if (!existing) {
        return [...prev, hydrated];
      }
      // Always re-hydrate from backend on (re)select — backend is the
      // source of truth. Preserves the in-progress edits made by the
      // current user only when re-selecting the SAME id without
      // intervening backend changes; otherwise backend wins.
      return prev.map((s) => (s.id === id ? hydrated : s));
    });
  }, []);

  // --- Mutations -----------------------------------------------------------
  const handleAdd = useCallback(() => {
    // Generate id and name OUTSIDE the setSignals updater so side effects
    // (the createSignal API call) are NOT triggered twice by React 18
    // StrictMode, which intentionally invokes state updater functions twice
    // in development to surface inadvertent side effects.
    const id = (globalThis.crypto && globalThis.crypto.randomUUID)
      ? globalThis.crypto.randomUUID()
      : `sig-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const name = nextSignalName(signalsRef.current);
    const newSig = {
      id,
      name,
      inputs: [],
      rules: emptyRules(),
      // v4 bullet #7: new signals get dont_repeat=true by default.
      settings: defaultSettings(),
      doc: '',
    };
    setSignals((prev) => [...prev, newSig]);
    setSelectedId(id);
    setError(null);
    setLastResult(null);
    // Persist to backend in current category; surface success/failure via
    // the one-shot status indicator. Local signal still usable on failure.
    setOneshotStatus('saving');
    createSignal({
      id,
      name,
      category: persistedCategory,
      inputs: [],
      rules: emptyRules(),
      settings: defaultSettings(),
      description: '',
    }).then(() => {
      setOneshotStatus('saved');
      fetchPersistedSignals(persistedCategory);
    }).catch(() => {
      setOneshotStatus('error');
    });
  }, [persistedCategory, fetchPersistedSignals]);

  const handleDelete = useCallback((id) => {
    setConfirmDeleteId(id);
  }, []);

  const handleConfirmDelete = useCallback(() => {
    const id = confirmDeleteId;
    setConfirmDeleteId(null);
    if (!id) return;
    setSignals((prev) => {
      const next = prev.filter((s) => s.id !== id);
      setSelectedId((sel) => {
        if (sel !== id) return sel;
        return next.length > 0 ? next[0].id : null;
      });
      return next;
    });
    // Archive on backend (soft-delete → ARCHIVE category); surface result.
    setOneshotStatus('saving');
    archiveSignal(id).then(() => {
      setOneshotStatus('saved');
      fetchPersistedSignals(persistedCategory);
    }).catch(() => {
      setOneshotStatus('error');
    });
  }, [confirmDeleteId, persistedCategory, fetchPersistedSignals]);

  // Move a persisted signal to a different category. Preserves all
  // editable content via the full-replace PUT.
  const handleChangeItemCat = useCallback(async (id, newCat) => {
    const target = persistedSignals.find((s) => s.id === id);
    if (!target) return;
    setOneshotStatus('saving');
    try {
      await updateSignal(id, {
        name: target.name,
        category: newCat,
        inputs: target.inputs || [],
        rules: target.rules || {},
        settings: target.settings || {},
        description: target.description || '',
      });
      setOneshotStatus('saved');
      // If the new category differs from the current filter, the item
      // disappears from the current view — re-fetch to reflect backend truth.
      fetchPersistedSignals(persistedCategory);
    } catch {
      setOneshotStatus('error');
    }
  }, [persistedSignals, persistedCategory, fetchPersistedSignals]);

  const handleRename = useCallback((id, newName) => {
    setSignals((prev) => prev.map((s) => (s.id !== id ? s : { ...s, name: newName })));
    // The debounced backend autosave below will pick up the name change
    // via the payload (it's part of the dirty-tracked currentSelectedDoc).
  }, []);

  const handleInputsChange = useCallback((nextInputs) => {
    setSignals((prev) => prev.map((s) => (
      s.id !== selectedId ? s : { ...s, inputs: nextInputs }
    )));
  }, [selectedId]);

  const handleRulesChange = useCallback((nextRules) => {
    setSignals((prev) => prev.map((s) => (s.id !== selectedId ? s : { ...s, rules: nextRules })));
  }, [selectedId]);

  const handleDocChange = useCallback((nextDoc) => {
    setSignals((prev) => prev.map((s) => (s.id !== selectedId ? s : { ...s, doc: nextDoc })));
  }, [selectedId]);

  // --- Backend debounced auto-save for the selected persisted signal -------
  // Only fires when:
  //   - a signal is selected
  //   - that signal exists in the backend persisted list
  //   - the user has actually edited something
  // The payload is stringified so a re-render with structurally equal
  // content does not retrigger the debounce.
  const selectedDocSerialized = useMemo(() => {
    if (!selectedSignal || !selectedPersisted) return null;
    return JSON.stringify({
      name: selectedSignal.name,
      category: selectedPersisted.category,
      inputs: selectedSignal.inputs,
      rules: selectedSignal.rules,
      settings: selectedSignal.settings,
      description: selectedSignal.doc,
    });
  }, [selectedSignal, selectedPersisted]);

  // Track the "last seen from backend" snapshot per selectedId to
  // suppress the FIRST autosave cycle after a hydrate-on-select (which
  // would otherwise PUT the freshly fetched content back uselessly).
  // Seeded in an effect — never mutate refs during render.
  const lastHydratedPayloadRef = useRef({ id: null, payload: null });
  useEffect(() => {
    if (selectedPersisted && selectedPersisted.id === selectedId) {
      lastHydratedPayloadRef.current = {
        id: selectedId,
        payload: JSON.stringify({
          name: selectedPersisted.name,
          category: selectedPersisted.category,
          inputs: selectedPersisted.inputs || [],
          rules: selectedPersisted.rules || {},
          settings: selectedPersisted.settings || {},
          description: selectedPersisted.description || '',
        }),
      };
    } else if (selectedId === null) {
      lastHydratedPayloadRef.current = { id: null, payload: null };
    }
    // We deliberately depend on selectedId only — when ``selectedPersisted``
    // first appears in the list for the currently selected id, this effect
    // re-runs because ``selectedPersisted`` is in the deps below.
  }, [selectedId, selectedPersisted]);

  const backendDirty = !!selectedDocSerialized
    && (lastHydratedPayloadRef.current.id !== selectedId
        || lastHydratedPayloadRef.current.payload !== selectedDocSerialized);

  const handleBackendSave = useCallback(async (payloadStr) => {
    if (!selectedId || !payloadStr) return;
    const body = JSON.parse(payloadStr);
    await updateSignal(selectedId, body);
    // After a successful save, set last-hydrated to the just-sent payload
    // so the same content doesn't immediately re-trigger the debounce.
    lastHydratedPayloadRef.current = { id: selectedId, payload: payloadStr };
    // Refresh the persisted list so the local cache stays in sync (esp.
    // ``updated_at`` and, more importantly, the canonical content if the
    // backend coerced anything).
    fetchPersistedSignals(persistedCategory);
  }, [selectedId, persistedCategory, fetchPersistedSignals]);

  const {
    status: cloudStatus,
    reset: resetCloudStatus,
  } = useBackendAutosave({
    enabled: !!selectedPersisted && backendDirty,
    payload: selectedDocSerialized,
    onSave: handleBackendSave,
  });

  // When the selection changes, reset cloud status so the indicator
  // doesn't show "saved" for the previously selected signal.
  useEffect(() => {
    resetCloudStatus();
  }, [selectedId, resetCloudStatus]);

  // --- Validation + run ----------------------------------------------------
  // Run gate checks inputs too — every input must be configured
  // (instrument picked), every block's input_id must resolve to one of
  // them, every operand's input_id must resolve, every condition must
  // be complete.
  const { runDisabledReason, missingIds } = useMemo(
    () => computeRunGate(selectedSignal, availableIndicators),
    [selectedSignal, availableIndicators],
  );

  const canRun = !!selectedSignal && !running && runDisabledReason === null;

  const handleRun = useCallback(async () => {
    if (!selectedSignal) return;
    const { body, missing } = buildComputeRequestBody(selectedSignal, availableIndicators);
    if (missing.length > 0) {
      setError({
        error_type: 'validation',
        message: `Missing indicator spec(s): ${missing.join(', ')}.`,
      });
      setLastResult(null);
      return;
    }
    setError(null);
    await runAbortable(async ({ signal }) => {
      try {
        const data = await computeSignal(body.spec, body.indicators, { signal });
        if (signal.aborted) return;
        setLastResult(data);
      } catch (e) {
        if (signal.aborted) return;
        if (e && typeof e === 'object' && 'status' in e) {
          setError(normalizeErrorEnvelope(e.body, e.message || 'Request failed'));
          setLastResult(null);
        } else {
          const classified = classifyFetchError(e);
          const error_type = fetchKindToErrorType(classified.kind);
          if (error_type === ABORTED) {
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
  }, [selectedSignal, availableIndicators, runAbortable]);

  // Cancel any in-flight run and clear stale results when switching signals.
  useEffect(() => {
    abortRun();
    setLastResult(null);
    setError(null);
  }, [selectedId, abortRun]);

  // Drive the grid results-row height from the number of ownPanel indicators
  // so the row grows and the flex chain inside fills it naturally.
  const ownPanelCount = useMemo(() => countOwnPanelIndicators(lastResult), [lastResult]);
  const resultsRowMin = 972 + ownPanelCount * 250;

  const statsInputs = useMemo(
    () => buildSignalStatsInputs(lastResult, capital),
    [lastResult, capital],
  );
  const statsKey = statsInputs
    ? `${selectedSignal?.id ?? 'signal'}|${capital}|${statsInputs.dates.length}|${statsInputs.dates[0]}|${statsInputs.dates[statsInputs.dates.length - 1]}`
    : null;

  const exitDescriptions = useMemo(() => {
    const out = {};
    const exits = selectedSignal?.rules?.exits;
    if (Array.isArray(exits)) {
      for (const b of exits) {
        if (b && b.id) out[b.id] = typeof b.description === 'string' ? b.description : '';
      }
    }
    return out;
  }, [selectedSignal]);

  const entryDescriptions = useMemo(() => {
    const out = {};
    const entries = selectedSignal?.rules?.entries;
    if (Array.isArray(entries)) {
      for (const b of entries) {
        if (b && b.id) out[b.id] = typeof b.description === 'string' ? b.description : '';
      }
    }
    return out;
  }, [selectedSignal]);

  return (
    <div className={styles.page} style={{ '--results-row-min': `${resultsRowMin}px` }}>
      <div className={styles.listPanel}>
        <SignalsList
          signals={filteredPersistedSignals}
          selectedId={selectedId}
          onSelect={handleSelectPersisted}
          onAdd={handleAdd}
          onDelete={handleDelete}
          onRename={handleRename}
          search={search}
          onSearchChange={setSearch}
          category={persistedCategory}
          onCategoryChange={setPersistedCategory}
          onChangeItemCat={handleChangeItemCat}
          loading={persistedLoading}
        />
      </div>
      <div className={styles.editorPanel}>
        {selectedSignal ? (
          <>
            <InputsPanel
              inputs={selectedSignal.inputs || []}
              onChange={handleInputsChange}
            />
            <BlockEditor
              rules={selectedSignal.rules}
              onRulesChange={handleRulesChange}
              inputs={selectedSignal.inputs || []}
              indicators={availableIndicators}
              doc={selectedSignal.doc || ''}
              onDocChange={handleDocChange}
            />
          </>
        ) : (
          <div className={styles.editorEmpty}>
            Select a signal on the left, or click <strong>+ New</strong> to create one.
          </div>
        )}
      </div>
      <div className={styles.paramsPanel}>
        <div className={styles.paramsTopBar}>
          <SaveControls
            dirty={dirty}
            autosave={autosave}
            onSave={commitSave}
            onToggleAutosave={setAutosave}
            leftSlot={
              <>
                <InlineNameInput
                  entity={selectedSignal}
                  onRename={handleRename}
                  className={styles.nameInput}
                  placeholder="Select a signal"
                  selectedPlaceholder="Signal name"
                  ariaLabel="Signal name"
                />
                {(oneshotStatus !== 'idle' || selectedPersisted) && (
                  <SaveStatus
                    status={oneshotStatus !== 'idle' ? oneshotStatus : cloudStatus}
                    label="Cloud"
                  />
                )}
              </>
            }
          />
        </div>
        <ParamsPanel
          signal={selectedSignal}
          onRun={handleRun}
          running={running}
          canRun={canRun}
          runDisabledReason={runDisabledReason}
          capital={capital}
          onCapitalChange={setCapital}
        />
      </div>
      <div className={styles.chartPanel}>
        <Card
          title="Results"
          className={styles.resultsCard}
          bodyClassName={styles.resultsCardBody}
          data-testid="signal-results-card"
        >
          <ResultsView
            result={lastResult}
            loading={running}
            error={error}
            capital={capital}
            noRepeat={selectedSignal?.settings?.dont_repeat ?? true}
            signalRules={selectedSignal?.rules ?? null}
            availableIndicators={availableIndicators}
          />
        </Card>
      </div>
      {statsInputs && (
        <div className={styles.statsPanel} data-testid="signal-statistics">
          <Statistics
            key={statsKey}
            dates={statsInputs.dates}
            equity={statsInputs.equity}
            defaultRiskFreeRate={getRiskFreeRateFraction()}
          />
        </div>
      )}
      {lastResult && (
        <div className={styles.tradesPanel}>
          <TradeLog
            trades={Array.isArray(lastResult.trades) ? lastResult.trades : []}
            timestamps={Array.isArray(lastResult.timestamps) ? lastResult.timestamps : []}
            positions={Array.isArray(lastResult.positions) ? lastResult.positions : []}
            exitDescriptions={exitDescriptions}
            entryDescriptions={entryDescriptions}
          />
        </div>
      )}
      <ConfirmDialog
        open={confirmDeleteId !== null}
        title="Delete signal?"
        message="The signal and all its blocks will be permanently removed."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        destructive
        onConfirm={handleConfirmDelete}
        onCancel={() => setConfirmDeleteId(null)}
      />
    </div>
  );
}

export default SignalsPage;
