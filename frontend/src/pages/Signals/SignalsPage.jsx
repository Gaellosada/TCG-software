import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import SignalsList from './SignalsList';
import BlockEditor from './BlockEditor';
import ParamsPanel from './ParamsPanel';
import SignalChart from './SignalChart';
import ConfirmDialog from '../../components/ConfirmDialog';
import InputsPanel from './InputsPanel';
import { loadState, saveState, emptyRules } from './storage';
import { AUTOSAVE_KEY } from './storageKeys';
import { computeSignal } from '../../api/signals';
import { buildComputeRequestBody } from './requestBuilder';
import { isBlockRunnable, isInputConfigured } from './blockShape';
import { classifyFetchError } from '../../utils/fetchError';
import { coerceErrorType, fetchKindToErrorType, ABORTED } from '../Indicators/errorTaxonomy';
// Reuse the Indicators page's storage + param parser so referenced
// indicator specs can be shipped alongside a signal compute request.
import { loadState as loadIndicatorState } from '../Indicators/storage';
import { parseIndicatorSpec, reconcileParams, reconcileSeriesMap } from '../Indicators/paramParser';
import { DEFAULT_INDICATORS } from '../Indicators/defaultIndicators';
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

/**
 * Hydrate the list of indicators the user has access to. Pulls BOTH:
 *   - default indicators from the registry (hydrated with per-session
 *     overrides from the Indicators localStorage);
 *   - user-authored indicators from that same storage.
 *
 * Returns an array of ``{id, name, code, params, seriesMap}`` — the exact
 * shape the backend ``/api/signals/compute`` request needs for each
 * referenced indicator (we ship these wholesale). ``readonly`` flag is
 * preserved so the OperandPicker dropdown can show all of them.
 */
export function hydrateAvailableIndicators() {
  const saved = loadIndicatorState();
  const defaults = DEFAULT_INDICATORS.map((def) => {
    const savedEntry = saved.defaultState?.[def.id] || {};
    const spec = parseIndicatorSpec(def.code);
    return {
      id: def.id,
      name: def.name,
      code: def.code,
      readonly: true,
      params: reconcileParams(savedEntry.params || {}, spec.params),
      seriesMap: reconcileSeriesMap(savedEntry.seriesMap || {}, spec.seriesLabels),
    };
  });
  const userIndicators = (saved.indicators || []).map((ind) => {
    const spec = parseIndicatorSpec(ind.code || '');
    return {
      id: ind.id,
      name: ind.name,
      code: ind.code || '',
      readonly: false,
      params: reconcileParams(ind.params || {}, spec.params),
      seriesMap: reconcileSeriesMap(ind.seriesMap || {}, spec.seriesLabels),
    };
  });
  return [...defaults, ...userIndicators];
}

function SignalsPage() {
  const [signals, setSignals] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [search, setSearch] = useState('');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [lastResult, setLastResult] = useState(null);
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

  return (
    <div className={styles.page}>
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
        />
      </div>
      <div className={styles.chartPanel}>
        <Card
          title="Results"
          className={styles.resultsCard}
          bodyClassName={styles.resultsCardBody}
          data-testid="signal-results-card"
        >
          <SignalChart result={lastResult} loading={running} error={error} />
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
