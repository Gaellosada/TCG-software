import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import SignalsList from './SignalsList';
import BlockEditor from './BlockEditor';
import ParamsPanel from './ParamsPanel';
import ResultsView from './ResultsView';
import ConfirmDialog from '../../components/ConfirmDialog';
import InputsPanel from './InputsPanel';
import { loadState, saveState, emptyRules } from './storage';
import { AUTOSAVE_KEY } from './storageKeys';
import { computeSignal } from '../../api/signals';
import { buildComputeRequestBody } from './requestBuilder';
import { isBlockRunnable, isInputConfigured } from './blockShape';
import { countOwnPanelIndicators } from './resultsPlotTraces';
import { classifyFetchError } from '../../utils/fetchError';
import { coerceErrorType, fetchKindToErrorType, ABORTED } from '../Indicators/errorTaxonomy';
import { hydrateAvailableIndicators } from './hydrateIndicators';
import SaveControls, { useAutosave } from '../../components/SaveControls';
import Card from '../../components/Card';
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

function normalizeErrorEnvelope(body, fallbackStatusText) {
  if (!body || typeof body !== 'object') {
    return { error_type: 'validation', message: fallbackStatusText || 'Request failed' };
  }
  const error_type = coerceErrorType(body.error_type);
  const message = (typeof body.message === 'string' && body.message)
    || (typeof body.detail === 'string' && body.detail)
    || fallbackStatusText
    || 'Request failed';
  const out = { error_type, message };
  if (typeof body.traceback === 'string' && body.traceback) {
    out.traceback = body.traceback;
  }
  return out;
}

// Stable serialisation for dirty comparison — JSON.stringify over the
// exact shape we'd persist.
function serializePersistablePayload(signals) {
  return JSON.stringify({ signals });
}

// Re-export for consumers that import from this file.
export { hydrateAvailableIndicators } from './hydrateIndicators';

function SignalsPage() {
  const [signals, setSignals] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [search, setSearch] = useState('');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [lastResult, setLastResult] = useState(null);
  const [capital, setCapital] = useState(1000);
  const [noRepeat, setNoRepeat] = useState(false);
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

  const signalsRef = useRef(signals);
  signalsRef.current = signals;

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

  const filteredSignals = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return signals;
    return signals.filter((s) => (s.name || '').toLowerCase().includes(q));
  }, [signals, search]);

  // --- Mutations -----------------------------------------------------------
  const handleAdd = useCallback(() => {
    setSignals((prev) => {
      const id = (globalThis.crypto && globalThis.crypto.randomUUID)
        ? globalThis.crypto.randomUUID()
        : `sig-${Date.now()}-${Math.random()}`;
      const newSig = {
        id,
        name: nextSignalName(prev),
        inputs: [],
        rules: emptyRules(),
        doc: '',
      };
      setSelectedId(id);
      return [...prev, newSig];
    });
    setError(null);
    setLastResult(null);
  }, []);

  // Iter-3 (guardrail 11): replace window.confirm with ConfirmDialog.
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
  }, [confirmDeleteId]);

  const handleRename = useCallback((id, newName) => {
    setSignals((prev) => prev.map((s) => (s.id !== id ? s : { ...s, name: newName })));
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

  // --- Validation + run ----------------------------------------------------
  // v3: Run gate checks inputs too — every input must be configured
  // (instrument picked), every block's input_id must resolve to one of
  // them, every operand's input_id must resolve, every condition must
  // be complete.
  const { runDisabledReason, missingIds } = useMemo(() => {
    if (!selectedSignal) return { runDisabledReason: 'Select a signal first', missingIds: [] };
    const inputs = Array.isArray(selectedSignal.inputs) ? selectedSignal.inputs : [];
    if (inputs.length === 0) {
      return {
        runDisabledReason: 'Add at least one input at the top of the page.',
        missingIds: [],
      };
    }
    // Every input that's referenced by the rules must be configured;
    // conservatively require every declared input to be configured so
    // there's no dangling-instrument UX.
    for (const input of inputs) {
      if (!isInputConfigured(input)) {
        return {
          runDisabledReason: `Input "${input.id}" needs an instrument — open the Inputs panel to pick one.`,
          missingIds: [],
        };
      }
    }
    const rules = selectedSignal.rules || {};
    const blocksWithDir = Object.keys(rules).flatMap((dir) => {
      const blocks = Array.isArray(rules[dir]) ? rules[dir] : [];
      return blocks.map((b) => ({ block: b, direction: dir }));
    });
    const nonEmpty = blocksWithDir.filter(({ block: b }) => (
      (b.conditions || []).length > 0 || b.input_id
    ));
    if (nonEmpty.length === 0) {
      return {
        runDisabledReason: 'Add at least one block with an input + condition',
        missingIds: [],
      };
    }
    // Entry blocks require at least one matching exit block.
    const hasLongEntry = (rules.long_entry || []).length > 0;
    const hasLongExit = (rules.long_exit || []).length > 0;
    const hasShortEntry = (rules.short_entry || []).length > 0;
    const hasShortExit = (rules.short_exit || []).length > 0;
    if (hasLongEntry && !hasLongExit) {
      return {
        runDisabledReason: 'Long entry blocks need at least one long exit block — add an exit condition so positions can close.',
        missingIds: [],
      };
    }
    if (hasShortEntry && !hasShortExit) {
      return {
        runDisabledReason: 'Short entry blocks need at least one short exit block — add an exit condition so positions can close.',
        missingIds: [],
      };
    }
    for (const { block: b, direction } of nonEmpty) {
      if (!b.input_id) {
        return {
          runDisabledReason: 'Every block needs an input — pick one in the block header.',
          missingIds: [],
        };
      }
      if (!(b.conditions || []).length) {
        return {
          runDisabledReason: 'Every block needs at least one condition.',
          missingIds: [],
        };
      }
      const isEntry = direction === 'long_entry' || direction === 'short_entry';
      if (isEntry && (!Number.isFinite(b.weight) || b.weight <= 0)) {
        return {
          runDisabledReason: 'Every entry block needs a positive weight — '
            + 'set a weight > 0 in the block header.',
          missingIds: [],
        };
      }
      if (!isBlockRunnable(b, direction, inputs)) {
        return {
          runDisabledReason: 'Every operand must be set — pick an input, '
            + 'indicator or constant for each slot.',
          missingIds: [],
        };
      }
    }
    const { missing } = buildComputeRequestBody(selectedSignal, availableIndicators);
    if (missing.length > 0) {
      return {
        runDisabledReason: `Missing indicator spec(s): ${missing.join(', ')}. `
          + 'Open the Indicators page to create them first.',
        missingIds: missing,
      };
    }
    return { runDisabledReason: null, missingIds: [] };
  }, [selectedSignal, availableIndicators]);

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
    setRunning(true);
    setError(null);
    try {
      const data = await computeSignal(body.spec, body.indicators);
      setLastResult(data);
    } catch (e) {
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
    } finally {
      setRunning(false);
    }
  }, [selectedSignal, availableIndicators]);

  // Drive the grid results-row height from the number of ownPanel indicators
  // so the row grows and the flex chain inside fills it naturally.
  const ownPanelCount = useMemo(() => countOwnPanelIndicators(lastResult), [lastResult]);
  const resultsRowMin = 972 + ownPanelCount * 250;

  return (
    <div className={styles.page} style={{ '--results-row-min': `${resultsRowMin}px` }}>
      <div className={styles.listPanel}>
        <SignalsList
          signals={filteredSignals}
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
              <SignalNameInput signal={selectedSignal} onRename={handleRename} />
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
          noRepeat={noRepeat}
          onNoRepeatChange={setNoRepeat}
        />
      </div>
      <div className={styles.chartPanel}>
        <Card
          title="Results"
          className={styles.resultsCard}
          bodyClassName={styles.resultsCardBody}
          data-testid="signal-results-card"
        >
          <ResultsView result={lastResult} loading={running} error={error} capital={capital} noRepeat={noRepeat} />
        </Card>
      </div>
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

function SignalNameInput({ signal, onRename }) {
  const [draft, setDraft] = useState(signal?.name || '');
  const prevIdRef = useRef(signal?.id);
  const focusedRef = useRef(false);

  useEffect(() => {
    if (prevIdRef.current !== signal?.id) {
      prevIdRef.current = signal?.id;
      setDraft(signal?.name || '');
    } else if ((signal?.name || '') !== draft && !focusedRef.current) {
      setDraft(signal?.name || '');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signal?.id, signal?.name]);

  function commit() {
    focusedRef.current = false;
    if (!signal) { setDraft(''); return; }
    const next = draft.trim();
    if (!next || next === signal.name) { setDraft(signal.name); return; }
    if (onRename) onRename(signal.id, next);
  }

  return (
    <input
      className={styles.nameInput}
      type="text"
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onFocus={() => { focusedRef.current = true; }}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur(); }
      }}
      disabled={!signal}
      placeholder={signal ? 'Signal name' : 'Select a signal'}
      aria-label="Signal name"
    />
  );
}

export default SignalsPage;
