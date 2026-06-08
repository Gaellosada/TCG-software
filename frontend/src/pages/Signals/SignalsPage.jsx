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
import { emptyRules, defaultSettings } from './storage';
import { AUTOSAVE_KEY } from './storageKeys';
import { computeSignal } from '../../api/signals';
import {
  listSignals, createSignal, updateSignal, archiveSignal,
  describePersistenceError,
} from '../../api/persistence';
import { buildComputeRequestBody } from './requestBuilder';
import { computeRunGate } from './runGate';
import { countOwnPanelIndicators } from './resultsPlotTraces';
import { classifyFetchError } from '../../utils/fetchError';
import { fetchKindToErrorType, ABORTED } from '../Indicators/errorTaxonomy';
import { normalizeErrorEnvelope } from '../../utils/errorEnvelope';
import { hydrateAvailableIndicators } from './hydrateIndicators';
import { hydrateFromPersisted } from './hydrateSignal';
import { getRiskFreeRateFraction } from '../../lib/userSettings';
import SaveControls from '../../components/SaveControls';
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
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);

  // --- Persistence state ---------------------------------------------------
  // signals is now the single source of truth, loaded from the backend.
  // persistedSignals has been removed — signals IS the persisted list.
  const [persistedCategory, setPersistedCategory] = useState('RESEARCH');
  const [persistedLoading, setPersistedLoading] = useState(false);
  // Error state for the initial backend fetch — shown when the backend is unreachable.
  const [fetchError, setFetchError] = useState(null);

  const signalsRef = useRef(signals);
  signalsRef.current = signals;

  // Separate status state for one-shot operations (add / archive /
  // category-change). Kept separate from the debounced autosave status so
  // neither path's timing can overwrite the other.
  const [oneshotStatus, setOneshotStatus] = useState('idle');
  // Detailed error message for one-shot persistence failures (M8).
  // Shown as a tooltip / inline subtext on SaveStatus.
  const [oneshotError, setOneshotError] = useState(null);
  // Detailed error message for the most recent debounced cloud autosave
  // failure (M8). Cleared when a save succeeds.
  const [cloudError, setCloudError] = useState(null);

  const setAutosave = useCallback((on) => {
    setAutosaveState(on);
    try { localStorage.setItem(AUTOSAVE_KEY, String(on)); } catch { /* ignore */ }
  }, []);

  // --- Fetch signals from backend ------------------------------------------
  const fetchSignals = useCallback(async (cat) => {
    setPersistedLoading(true);
    setFetchError(null);
    try {
      const docs = await listSignals(cat);
      const hydrated = docs.map(hydrateFromPersisted);
      setSignals(hydrated);
      // Select the first signal if nothing is selected, or keep current
      // selection if the signal still exists in the new list.
      setSelectedId((prev) => {
        if (prev && hydrated.find((s) => s.id === prev)) return prev;
        return hydrated.length > 0 ? hydrated[0].id : null;
      });
    } catch (err) {
      setFetchError(describePersistenceError(err));
      setSignals([]);
      setSelectedId(null);
    } finally {
      setPersistedLoading(false);
    }
  }, []);

  // --- Hydrate on mount ----------------------------------------------------
  useEffect(() => {
    fetchSignals(persistedCategory);
    hydrateAvailableIndicators().then(setAvailableIndicators);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-hydrate available indicators whenever the window regains focus --
  // catches edits made on the Indicators page without a reload.
  useEffect(() => {
    function refresh() { hydrateAvailableIndicators().then(setAvailableIndicators); }
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, []);

  // --- Re-fetch signals when category changes ------------------------------
  useEffect(() => {
    fetchSignals(persistedCategory);
  }, [persistedCategory, fetchSignals]);

  const selectedSignal = useMemo(
    () => signals.find((s) => s.id === selectedId) || null,
    [signals, selectedId],
  );

  // All signals are now loaded from the backend — filter the unified list for display.
  const filteredSignals = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return signals;
    return signals.filter((s) => (s.name || '').toLowerCase().includes(q));
  }, [signals, search]);

  // Mirror of ``backendDirty`` accessible from event handlers that are
  // declared before ``backendDirty`` itself is defined in the function
  // body. Synced via effect (see below).
  const backendDirtyRef = useRef(false);

  // Simple selection handler — all signals live in the unified `signals` state.
  // Guard against overwriting in-progress edits on re-click of the same row.
  const handleSelect = useCallback((id) => {
    if (id === selectedId && backendDirtyRef.current) {
      return;
    }
    setSelectedId(id);
  }, [selectedId]);

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
    // Optimistically add to local state so the UI feels instant.
    setSignals((prev) => [...prev, newSig]);
    setSelectedId(id);
    setError(null);
    setLastResult(null);
    // Persist to backend in current category; refresh from backend on success
    // to stay in sync (picks up created_at, updated_at, etc.).
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
      setOneshotError(null);
      setOneshotStatus('saved');
      fetchSignals(persistedCategory);
    }).catch((err) => {
      // M8: capture error details (status/message) so the user can see
      // what went wrong — not just an opaque "save failed".
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // Roll back — remove the optimistically added signal.
      setSignals((prev) => prev.filter((s) => s.id !== id));
      setSelectedId((sel) => sel === id ? null : sel);
      // eslint-disable-next-line no-console
      console.error('createSignal failed:', err);
    });
  }, [persistedCategory, fetchSignals]);

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
    // Archive on backend (soft-delete → ARCHIVE category); refresh from backend.
    setOneshotStatus('saving');
    // Capture the signal before removal so we can roll back on failure.
    const target = signalsRef.current.find((s) => s.id === id);
    archiveSignal(id).then(() => {
      setOneshotError(null);
      setOneshotStatus('saved');
      fetchSignals(persistedCategory);
    }).catch((err) => {
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // Roll back — re-add the optimistically removed signal.
      if (target) {
        setSignals((prev) => [...prev, target]);
      }
      // eslint-disable-next-line no-console
      console.error('archiveSignal failed:', err);
    });
  }, [confirmDeleteId, persistedCategory, fetchSignals]);

  // Move a signal to a different category. Preserves all editable
  // content via the full-replace PUT.
  const handleChangeItemCat = useCallback(async (id, newCat) => {
    const target = signalsRef.current.find((s) => s.id === id);
    if (!target) return;
    setOneshotStatus('saving');
    try {
      await updateSignal(id, {
        name: target.name,
        category: newCat,
        inputs: target.inputs || [],
        rules: target.rules || {},
        settings: target.settings || {},
        description: target.doc || '',
      });
      setOneshotError(null);
      setOneshotStatus('saved');
      // If the new category differs from the current filter, the item
      // disappears from the current view — re-fetch to reflect backend truth.
      fetchSignals(persistedCategory);
    } catch (err) {
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // eslint-disable-next-line no-console
      console.error('updateSignal (category change) failed:', err);
    }
  }, [persistedCategory, fetchSignals]);

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

  // --- Backend debounced auto-save for the selected signal -----------------
  // Fires when a signal is selected and the user has actually edited
  // something since the last save/fetch. The payload is stringified so a
  // re-render with structurally equal content does not retrigger the debounce.
  const selectedDocSerialized = useMemo(() => {
    if (!selectedSignal) return null;
    return JSON.stringify({
      name: selectedSignal.name,
      category: persistedCategory,
      inputs: selectedSignal.inputs,
      rules: selectedSignal.rules,
      settings: selectedSignal.settings,
      description: selectedSignal.doc,
    });
  }, [selectedSignal, persistedCategory]);

  // Track the "last seen from backend" snapshot per selectedId to
  // suppress the FIRST autosave cycle after a fetch/select (which
  // would otherwise PUT the freshly fetched content back uselessly).
  // Seeded in an effect — never mutate refs during render.
  //
  // Depends on ``selectedId`` so it fires on selection change. The
  // ``signals`` reference is NOT in the dependency list to avoid
  // re-seeding on every local edit. The handleBackendSave callback
  // updates the ref directly after a successful save.
  const lastHydratedPayloadRef = useRef({ id: null, payload: null });
  useEffect(() => {
    if (selectedSignal && selectedSignal.id === selectedId) {
      lastHydratedPayloadRef.current = {
        id: selectedId,
        payload: JSON.stringify({
          name: selectedSignal.name,
          category: persistedCategory,
          inputs: selectedSignal.inputs || [],
          rules: selectedSignal.rules || {},
          settings: selectedSignal.settings || {},
          description: selectedSignal.doc || '',
        }),
      };
    } else if (selectedId === null) {
      lastHydratedPayloadRef.current = { id: null, payload: null };
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  const backendDirty = !!selectedDocSerialized
    && (lastHydratedPayloadRef.current.id !== selectedId
        || lastHydratedPayloadRef.current.payload !== selectedDocSerialized);
  // Keep the ref in sync so ``handleSelect`` (declared earlier) can read
  // the current dirty state without a closure dependency.
  backendDirtyRef.current = backendDirty;

  const handleBackendSave = useCallback(async (payloadStr, { signal } = {}) => {
    if (!selectedId || !payloadStr) return;
    const body = JSON.parse(payloadStr);
    try {
      await updateSignal(selectedId, body, { signal });
    } catch (err) {
      // Aborts are intentional cancellations — let the hook surface
      // them as 'idle'. Other errors: capture details for the UI tooltip
      // and re-throw so the hook moves to 'error'.
      if (err && err.name === 'AbortError') {
        throw err;
      }
      setCloudError(describePersistenceError(err));
      // eslint-disable-next-line no-console
      console.error('updateSignal (autosave) failed:', err);
      throw err;
    }
    // If the save was aborted between dispatch and resolution, stop here
    // — don't touch the hydrated ref or refetch.
    if (signal && signal.aborted) return;
    // Clear any prior error after a successful save.
    setCloudError(null);
    // After a successful save, set last-hydrated to the just-sent payload
    // so the same content doesn't immediately re-trigger the debounce.
    lastHydratedPayloadRef.current = { id: selectedId, payload: payloadStr };
    // Note: we intentionally do NOT re-fetch the full signal list after
    // every autosave — it would cause selection flicker and reset scroll.
    // The local state is authoritative until a category change or add/delete.
  }, [selectedId]);

  const {
    status: cloudStatus,
    flush: flushCloudSave,
    reset: resetCloudStatus,
  } = useBackendAutosave({
    enabled: autosave && !!selectedSignal && backendDirty,
    payload: selectedDocSerialized,
    onSave: handleBackendSave,
  });

  // Manual Save button: flush the pending backend autosave, or fire a
  // one-shot save when autosave is off.
  const commitSave = useCallback(() => {
    if (autosave) {
      flushCloudSave();
    } else {
      const payload = selectedDocSerialized;
      if (payload && selectedId) {
        handleBackendSave(payload, {}).catch(() => {
          // Error already set by handleBackendSave.
        });
      }
    }
  }, [autosave, flushCloudSave, selectedDocSerialized, selectedId, handleBackendSave]);

  // When the selection changes, reset cloud status so the indicator
  // doesn't show "saved" for the previously selected signal.
  useEffect(() => {
    resetCloudStatus();
  }, [selectedId, resetCloudStatus]);

  // M7: derive what the SaveStatus indicator should actually show.
  //
  // Precedence rules:
  //   1. If the debounced cloud autosave is actively 'saving', that
  //      wins — a stale 'saved' from a prior one-shot must not mask
  //      an in-flight save.
  //   2. If the debounced autosave is 'error', that also wins — the
  //      user needs to know an autosave failed even if a recent
  //      one-shot succeeded.
  //   3. Otherwise the more recent one-shot status takes precedence
  //      (e.g. just-clicked "+ New" → show 'saving' or 'error').
  //   4. Fallback to cloudStatus.
  const displayedSaveStatus = (
    cloudStatus === 'saving' || cloudStatus === 'error'
      ? cloudStatus
      : (oneshotStatus !== 'idle' ? oneshotStatus : cloudStatus)
  );
  const saveErrorMessage = (
    displayedSaveStatus === 'error'
      ? (cloudStatus === 'error' ? cloudError : oneshotError)
      : null
  );

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
          signals={filteredSignals}
          selectedId={selectedId}
          onSelect={handleSelect}
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
        {fetchError ? (
          <div className={styles.editorEmpty}>
            <strong>Failed to load signals:</strong> {fetchError}
          </div>
        ) : selectedSignal ? (
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
            dirty={backendDirty}
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
                {(oneshotStatus !== 'idle' || selectedSignal) && (
                  <SaveStatus
                    status={displayedSaveStatus}
                    label="Cloud"
                    errorMessage={
                      displayedSaveStatus === 'error' ? saveErrorMessage : null
                    }
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
