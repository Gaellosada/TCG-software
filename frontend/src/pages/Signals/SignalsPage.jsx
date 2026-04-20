import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import SignalsList from './SignalsList';
import BlockEditor from './BlockEditor';
import ParamsPanel from './ParamsPanel';
import SignalChart from './SignalChart';
import { loadState, saveState, emptyRules } from './storage';
import { AUTOSAVE_KEY } from './storageKeys';
import { computeSignal } from '../../api/signals';
import { buildComputeRequestBody } from './requestBuilder';
import { isConditionComplete } from './conditionOps';
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
      const newSig = { id, name: nextSignalName(prev), rules: emptyRules() };
      setSelectedId(id);
      return [...prev, newSig];
    });
    setError(null);
    setLastResult(null);
  }, []);

  const handleDelete = useCallback((id) => {
    // eslint-disable-next-line no-alert
    if (!window.confirm('Delete?')) return;
    setSignals((prev) => {
      const next = prev.filter((s) => s.id !== id);
      setSelectedId((sel) => {
        if (sel !== id) return sel;
        return next.length > 0 ? next[0].id : null;
      });
      return next;
    });
  }, []);

  const handleRename = useCallback((id, newName) => {
    setSignals((prev) => prev.map((s) => (s.id !== id ? s : { ...s, name: newName })));
  }, []);

  const handleRulesChange = useCallback((nextRules) => {
    setSignals((prev) => prev.map((s) => (s.id !== selectedId ? s : { ...s, rules: nextRules })));
  }, [selectedId]);

  // --- Validation + run ----------------------------------------------------
  const { runDisabledReason, missingIds } = useMemo(() => {
    if (!selectedSignal) return { runDisabledReason: 'Select a signal first', missingIds: [] };
    const totalBlocks = Object.values(selectedSignal.rules || {}).reduce(
      (sum, blocks) => sum + (Array.isArray(blocks) ? blocks.length : 0),
      0,
    );
    if (totalBlocks === 0) {
      return { runDisabledReason: 'Add at least one block', missingIds: [] };
    }
    // Iter-2: operands are unset (``null``) until the user picks. Keep Run
    // disabled if any condition still has an unset / incomplete operand —
    // otherwise the backend would 422 every time.
    for (const dir of Object.keys(selectedSignal.rules || {})) {
      const blocks = selectedSignal.rules[dir] || [];
      for (const block of blocks) {
        const conds = (block && block.conditions) || [];
        for (const cond of conds) {
          if (!isConditionComplete(cond)) {
            return {
              runDisabledReason: 'Every operand must be set — pick an indicator, '
                + 'instrument or constant for each slot.',
              missingIds: [],
            };
          }
        }
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
      // Two failure modes: (a) non-2xx with an envelope on ``e.body``;
      // (b) network-layer failure (no ``e.status``).
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
          <BlockEditor
            rules={selectedSignal.rules}
            onRulesChange={handleRulesChange}
            indicators={availableIndicators}
          />
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
