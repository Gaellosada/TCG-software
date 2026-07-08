import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import usePortfolio from './usePortfolio';
import HoldingsList from './HoldingsList';
import AddHoldingModal from './AddHoldingModal';
import SignalPickerModal from './SignalPickerModal';
import PersistedPortfolioPanel from './PersistedPortfolioPanel';
import TimeRangeSlider from '../../components/TimeRangeSlider';
import PortfolioEquityChart from './PortfolioEquityChart';
import ReturnsGrid from './ReturnsGrid';
import SaveControls from '../../components/SaveControls';
import SaveStatus from '../../components/SaveStatus/SaveStatus';
import useBackendAutosave from '../../hooks/useBackendAutosave';
import useEntityLock from '../../hooks/useEntityLock';
import ConfirmDialog from '../../components/ConfirmDialog';
import LockBanner from '../../components/LockBanner';
import Statistics from '../../components/Statistics';
import TradeLog from '../../components/TradeLog';
import styles from './PortfolioPage.module.css';
import { getRiskFreeRateFraction } from '../../lib/userSettings';
import {
  createPortfolio,
  updatePortfolio,
  archivePortfolio,
  setPortfolioLocked,
  describePersistenceError,
  isLockedError,
} from '../../api/persistence';
import { usePortfoliosList, useInvalidatePersistence } from '../../hooks/persistenceQueries';

// Portfolio API returns dates as ISO ``YYYY-MM-DD`` strings; the
// Statistics endpoint expects YYYYMMDD integers (existing project
// convention). Convert at the call site — the Statistics contract is
// shared and must not bend to one caller's format.
function isoDatesToYYYYMMDD(isoDates) {
  if (!Array.isArray(isoDates)) return null;
  const out = new Array(isoDates.length);
  for (let i = 0; i < isoDates.length; i++) {
    const s = isoDates[i];
    if (typeof s !== 'string' || s.length !== 10) return null;
    const n = Number(s.slice(0, 4) + s.slice(5, 7) + s.slice(8, 10));
    if (!Number.isFinite(n)) return null;
    out[i] = n;
  }
  return out;
}

const REBALANCE_OPTIONS = [
  { value: 'none', label: 'None' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
  { value: 'quarterly', label: 'Quarterly' },
  { value: 'annually', label: 'Annually' },
];

function PortfolioPage() {
  const portfolio = usePortfolio();
  const [modalOpen, setModalOpen] = useState(false);
  // Index of the leg whose instrument config is being edited (null = not
  // editing). Drives the shared AddHoldingModal into edit mode. Kept separate
  // from modalOpen so the add (append) flow is untouched.
  const [editLegIndex, setEditLegIndex] = useState(null);
  const [signalModalOpen, setSignalModalOpen] = useState(false);
  const [saveInput, setSaveInput] = useState('');
  // archiveTarget holds the backend ID of the portfolio pending-archive
  // (null = dialog closed). Replaces the old localStorage deleteTarget.
  const [archiveTarget, setArchiveTarget] = useState(null);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);

  // --- Portfolio list (backend is the sole source of truth) ----------------
  // The category filter comes from the hook (shared with the autosave payload).
  // The list is a TanStack query keyed by that category; ``portfolios`` (local
  // state) is kept as the page's working copy (lock-flag patches, etc.) and is
  // re-synced from the query whenever a fresh snapshot lands. A mutation calls
  // invalidate.portfolios() → background refetch → re-sync.
  const portfoliosQuery = usePortfoliosList(portfolio.persistedCategory);
  const invalidate = useInvalidatePersistence();
  const [portfolios, setPortfolios] = useState([]);

  // Separate status state for one-shot operations (save-current / archive /
  // category-change). Kept separate from the debounced autosave status so
  // neither path's timing can overwrite the other.
  const [oneshotStatus, setOneshotStatus] = useState('idle');
  // M8: detailed error message for one-shot persistence failures.
  const [oneshotError, setOneshotError] = useState(null);
  // M8: detailed error message for the most recent debounced cloud
  // autosave failure. Cleared when a save succeeds.
  const [cloudError, setCloudError] = useState(null);

  // Loading / error derived from the query. ``portfoliosLoading`` reflects
  // only the first (cold) load — a background refetch with cached data must
  // NOT flip the panel into a loading state (no-flicker, matches prior code).
  const portfoliosLoading = portfoliosQuery.isPending && portfoliosQuery.fetchStatus !== 'idle';
  const fetchError = portfoliosQuery.error
    ? `Failed to load portfolios: ${portfoliosQuery.error.message || portfoliosQuery.error}`
    : null;

  // Re-sync the working list whenever the query lands a snapshot. (Category
  // changes are automatic: the query is keyed by persistedCategory.)
  useEffect(() => {
    if (portfoliosQuery.data) setPortfolios(portfoliosQuery.data);
  }, [portfoliosQuery.data]);

  // Serialize the portfolio leg list into the wire shape — strip the
  // local-only ``id`` (which we assign on load and never persist) and
  // null-out missing fields so the backend receives a clean shape.
  const legsToWire = useCallback((legs) => legs.map((l) => ({
    label: l.label,
    type: l.type || 'instrument',
    collection: l.collection || null,
    symbol: l.symbol || null,
    strategy: l.strategy || null,
    adjustment: l.adjustment || null,
    cycle: l.cycle || null,
    rollOffset: l.rollOffset ?? 0,
    weight: l.weight ?? 0,
    signalId: l.signalId || null,
    signalName: l.signalName || null,
    signalSpec: l.signalSpec || null,
    option_type: l.option_type || null,
    maturity: l.maturity || null,
    selection: l.selection || null,
    stream: l.stream || null,
    // option_stream roll offset — the unified {value, unit} object (snake_case,
    // distinct from the futures leg's camelCase `rollOffset` above). The shared
    // `adjustment` field applies to continuous (futures) legs only. ("End of
    // month" is the maturity, not a separate roll_schedule — that was removed.)
    roll_offset: l.roll_offset ?? null,
    // SELECT-AND-HOLD (fixed-contract dollar-P&L) — option_stream legs only.
    hold_between_rolls: l.hold_between_rolls ?? false,
    nav_times: l.nav_times ?? 1.0,
    // Option hold-mode SIZING pass-through. These were dropped here, so a
    // portfolio option leg ALWAYS fell back to the backend default
    // ``premium_notional`` — which wipes out a low-premium (e.g. 10Δ) leg
    // (qty = NAV/premium ⇒ enormous leverage). Emit ONLY when set so an
    // untouched leg stays byte-identical AND the backend applies its defaults
    // (``sizing_mode`` is a non-optional Literal — never send null).
    ...(l.sizing_mode ? { sizing_mode: l.sizing_mode } : {}),
    ...(l.futures_reference ? { futures_reference: l.futures_reference } : {}),
  })), []);

  // Save current portfolio state to backend in the selected category.
  // After a successful create, takes over autosave by setting
  // ``persistedId`` so ongoing edits get debounced PUTs.
  const handleCreatePortfolio = useCallback(async () => {
    const name = saveInput.trim() || portfolio.portfolioName || 'Portfolio';
    const id = `portfolio-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const category = portfolio.persistedCategory;
    setOneshotStatus('saving');
    try {
      await createPortfolio({
        id,
        name,
        category,
        legs: legsToWire(portfolio.legs),
        rebalance: portfolio.rebalance || 'none',
      });
      setOneshotError(null);
      setOneshotStatus('saved');
      portfolio.setPersistedId(id);
      portfolio.setPersistedCategory(category);
      // Make sure the portfolioName state reflects what we just saved
      // so subsequent autosaves include it.
      if (name !== portfolio.portfolioName) {
        portfolio.setPortfolioName(name);
      }
      invalidate.portfolios(id);
    } catch (err) {
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // eslint-disable-next-line no-console
      console.error('createPortfolio failed:', err);
    }
  }, [
    saveInput,
    portfolio.portfolioName, portfolio.persistedCategory, portfolio.legs, portfolio.rebalance,
    portfolio.setPersistedId, portfolio.setPersistedCategory, portfolio.setPortfolioName,
    invalidate, legsToWire,
  ]);

  // Move a persisted portfolio to a different category. Preserves all
  // editable content via the full-replace PUT.
  const handleChangePortfolioCat = useCallback(async (id, newCat) => {
    const target = portfolios.find((p) => p.id === id);
    if (!target) return;
    setOneshotStatus('saving');
    try {
      await updatePortfolio(id, {
        name: target.name,
        category: newCat,
        legs: target.legs || [],
        rebalance: target.rebalance || 'none',
      });
      setOneshotError(null);
      setOneshotStatus('saved');
      // If this is the currently loaded portfolio, update its category
      // in the hook so the autosave payload stays correct.
      if (portfolio.persistedId === id) {
        portfolio.setPersistedCategory(newCat);
      }
      // The doc moved between categories — invalidate so BOTH the old and new
      // category lists refetch (prefix match covers every category). If the
      // loaded portfolio moved, setPersistedCategory above also re-keys the
      // panel query to the new category.
      invalidate.portfolios(id);
    } catch (err) {
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // eslint-disable-next-line no-console
      console.error('updatePortfolio (category change) failed:', err);
    }
  }, [portfolios, portfolio.persistedId, portfolio.persistedCategory, portfolio.setPersistedCategory, invalidate]);

  // Archive (soft-delete) a persisted portfolio.
  const handleArchivePortfolio = useCallback(async (id) => {
    setOneshotStatus('saving');
    try {
      await archivePortfolio(id);
      setOneshotError(null);
      setOneshotStatus('saved');
      // If we were editing this exact portfolio, clear the editor so
      // further edits don't try to autosave to an archived row.
      if (portfolio.persistedId === id) {
        portfolio.clearAll();
      }
      invalidate.portfolios(id);
    } catch (err) {
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // eslint-disable-next-line no-console
      console.error('archivePortfolio failed:', err);
    }
  }, [portfolio.persistedId, portfolio.persistedCategory, portfolio.clearAll, invalidate]);

  // Set the locked state on a persisted portfolio. Calls the lock API,
  // then updates both the list state and (when it's the currently loaded
  // portfolio) the hook's locked flag so the read-only banner reacts.
  // Shared lock-handler hook (same shape across all three pages); this page
  // is server-confirmed (no optimistic flip) and also syncs the hook flag.
  const applyPortfolioLocked = useCallback((id, lockedVal) => {
    // Update the list row so the LockToggle and disabled states reflect
    // the new lock state immediately (same pattern as category change).
    setPortfolios((prev) =>
      prev.map((p) => (p.id === id ? { ...p, locked: lockedVal } : p)),
    );
    // If the currently loaded portfolio was just locked/unlocked, sync the
    // hook flag so the read-only builder banner updates.
    if (portfolio.persistedId === id) {
      portfolio.setPersistedLocked(!!lockedVal);
    }
  }, [portfolio.persistedId, portfolio.setPersistedLocked]);
  const handleSetPortfolioLocked = useEntityLock({
    // Lazy wrapper — defers the api import access to call time so test
    // mocks that omit setPortfolioLocked don't trip a render-time getter.
    setLocked: useCallback((id, next) => setPortfolioLocked(id, next), []),
    applyLocked: applyPortfolioLocked,
    onStart: useCallback(() => setOneshotStatus('saving'), []),
    onSuccess: useCallback((doc) => {
      setOneshotError(null);
      setOneshotStatus('saved');
      // applyLocked already patched the lock flag from the server doc; invalidate
      // to keep the cached list coherent (refetch returns equal data → no flicker).
      if (doc && doc.id) invalidate.portfolios(doc.id);
    }, [invalidate]),
    onError: useCallback((err) => {
      setOneshotError(describePersistenceError(err));
      setOneshotStatus('error');
      // eslint-disable-next-line no-console
      console.error('setPortfolioLocked failed:', err);
    }, []),
  });

  // Mirror of ``cloudDirty`` accessible from event handlers declared
  // before ``cloudDirty`` itself is defined (synced via assignment below).
  const cloudDirtyRef = useRef(false);

  // Load a backend-persisted portfolio into the editor.
  //
  // Guard against destroying in-progress edits: if the user clicks the
  // SAME persisted row that is currently loaded AND has unsaved local
  // edits (the debounce hasn't fired yet), DO NOT overwrite local
  // state with the stale backend snapshot. The autosave hook is
  // responsible for pushing those edits to the backend.
  const handleSelectPersisted = useCallback((id) => {
    if (id === portfolio.persistedId && cloudDirtyRef.current) {
      return;
    }
    const doc = portfolios.find((p) => p.id === id);
    if (!doc) return;
    portfolio.loadFromPersisted(doc);
    setSaveInput(doc.name || '');
  }, [portfolios, portfolio.persistedId, portfolio.loadFromPersisted]);

  // --- Backend debounced auto-save for the loaded persisted portfolio -----
  // The category is now tracked inside the hook (portfolio.persistedCategory)
  // so we no longer need to look it up from the portfolios list.
  const cloudPayload = useMemo(() => {
    if (!portfolio.persistedId) return null;
    return JSON.stringify({
      name: portfolio.portfolioName || 'Portfolio',
      category: portfolio.persistedCategory,
      legs: legsToWire(portfolio.legs),
      rebalance: portfolio.rebalance || 'none',
    });
  }, [
    portfolio.persistedId,
    portfolio.portfolioName,
    portfolio.persistedCategory,
    portfolio.legs,
    portfolio.rebalance,
    legsToWire,
  ]);

  // Track the snapshot we last received from the backend so we don't
  // immediately PUT back the freshly-hydrated content.
  const lastSeenPayloadRef = useRef({ id: null, payload: null });
  useEffect(() => {
    if (!portfolio.persistedId) {
      lastSeenPayloadRef.current = { id: null, payload: null };
      return;
    }
    // Snapshot the initial state from the backend list so that a
    // freshly-loaded portfolio doesn't immediately trigger a PUT.
    const persisted = portfolios.find((p) => p.id === portfolio.persistedId);
    if (!persisted) return;
    if (lastSeenPayloadRef.current.id !== portfolio.persistedId) {
      lastSeenPayloadRef.current = {
        id: portfolio.persistedId,
        payload: JSON.stringify({
          name: persisted.name,
          category: persisted.category,
          legs: persisted.legs || [],
          rebalance: persisted.rebalance || 'none',
        }),
      };
    }
  }, [portfolio.persistedId, portfolios]);

  const cloudDirty = !!cloudPayload
    && (lastSeenPayloadRef.current.id !== portfolio.persistedId
        || lastSeenPayloadRef.current.payload !== cloudPayload);
  // Keep the ref in sync so ``handleSelectPersisted`` (declared earlier)
  // can read the current dirty state without a closure dependency.
  cloudDirtyRef.current = cloudDirty;

  // Ref to the autosave hook's reset() so the locked-save handler (declared
  // before the hook) can clear a transient 'saving'/'error' status when it
  // flips the portfolio to locked. Seeded just below the hook.
  const resetCloudStatusRef = useRef(() => {});

  const handleCloudSave = useCallback(async (payloadStr, { signal } = {}) => {
    if (!portfolio.persistedId || !payloadStr) return;
    const body = JSON.parse(payloadStr);
    try {
      await updatePortfolio(portfolio.persistedId, body, { signal });
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      // 423 Locked: flip the LOCAL locked flag (hook flag + list row) so the
      // editor goes read-only with the normal lock banner instead of a
      // generic error. The hook's enabled gate already excludes locked, so
      // no re-fire loop. Also patch the matching list row's lock state.
      if (isLockedError(err)) {
        portfolio.setPersistedLocked(true);
        setPortfolios((prev) =>
          prev.map((p) => (p.id === portfolio.persistedId ? { ...p, locked: true } : p)),
        );
        setCloudError(null);
        resetCloudStatusRef.current();
        return;
      }
      setCloudError(describePersistenceError(err));
      // eslint-disable-next-line no-console
      console.error('updatePortfolio (autosave) failed:', err);
      throw err;
    }
    // If the save was aborted between dispatch and resolution, don't
    // mutate the last-seen ref or refetch.
    if (signal && signal.aborted) return;
    setCloudError(null);
    lastSeenPayloadRef.current = {
      id: portfolio.persistedId,
      payload: payloadStr,
    };
    // Note: we intentionally do NOT re-fetch the full portfolio list after
    // every autosave — it would cause flicker and reset scroll position
    // during rapid editing. The local state is authoritative until a
    // category change, add, or archive operation.
  }, [portfolio.persistedId, portfolio.persistedCategory, portfolio.setPersistedLocked]);

  const {
    status: cloudStatus,
    saveNow: saveNowCloud,
    reset: resetCloudStatus,
  } = useBackendAutosave({
    enabled: !!portfolio.persistedId && cloudDirty && portfolio.autosave && !portfolio.persistedLocked,
    payload: cloudPayload,
    onSave: handleCloudSave,
  });
  resetCloudStatusRef.current = resetCloudStatus;

  // Reset cloud status indicator on portfolio (de)selection.
  useEffect(() => {
    resetCloudStatus();
  }, [portfolio.persistedId, resetCloudStatus]);

  // M7: precedence — debounced cloud autosave 'saving'/'error' wins over
  // a stale one-shot 'saved'. Otherwise prefer the more recent one-shot.
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

  // Pre-fill save input when a portfolio is loaded
  useEffect(() => {
    if (portfolio.portfolioName) {
      setSaveInput(portfolio.portfolioName);
    }
  }, [portfolio.portfolioName]);

  const handleOpenModal = useCallback(() => setModalOpen(true), []);
  // Close covers BOTH the add flow (modalOpen) and the edit flow (editLegIndex).
  const handleCloseModal = useCallback(() => {
    setModalOpen(false);
    setEditLegIndex(null);
  }, []);
  // Open the shared holding modal in EDIT mode for a specific leg. The trigger
  // (HoldingsList instrument cell) is a role="button" that escapes the locked
  // <fieldset>, so this also fires for a locked portfolio — in which case the
  // modal opens read-only (view-only) per the picker contract.
  const handleEditLeg = useCallback((index) => setEditLegIndex(index), []);

  // "Save" button. Not yet persisted → create in the backend. Already
  // persisted → persist the CURRENT state IMMEDIATELY via ``saveNow``
  // (the old code was a no-op that relied entirely on autosave, so with
  // autosave off clicking Save saved nothing).
  const handleSave = useCallback(() => {
    if (portfolio.persistedLocked) return;
    if (!portfolio.persistedId) {
      // Not yet persisted — create in backend.
      handleCreatePortfolio();
      return;
    }
    // Apply a pending rename typed into the name input.
    const name = saveInput.trim();
    if (name && name !== portfolio.portfolioName) {
      portfolio.setPortfolioName(name);
    }
    // Persist now. ``setPortfolioName`` above is async (state has not
    // propagated into ``cloudPayload`` yet), so build the payload with
    // the just-entered name explicitly and hand it to ``saveNow`` to
    // avoid a stale-payload race.
    const overridePayload = JSON.stringify({
      name: name || portfolio.portfolioName || 'Portfolio',
      category: portfolio.persistedCategory,
      legs: legsToWire(portfolio.legs),
      rebalance: portfolio.rebalance || 'none',
    });
    saveNowCloud(overridePayload);
  }, [
    saveInput,
    portfolio.persistedId,
    portfolio.persistedLocked,
    portfolio.portfolioName,
    portfolio.persistedCategory,
    portfolio.legs,
    portfolio.rebalance,
    portfolio.setPortfolioName,
    legsToWire,
    handleCreatePortfolio,
    saveNowCloud,
  ]);

  return (
    <div className={styles.page}>
      <div className={styles.scroll}>
        {/* ── Header ── */}
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            <h2 className={styles.pageTitle}>Portfolio</h2>
            {/* New portfolio — detach from current and start fresh */}
            <button
              className={styles.newBtn}
              type="button"
              onClick={() => setClearConfirmOpen(true)}
              disabled={portfolio.legs.length === 0 && !portfolio.persistedId}
              title="New portfolio"
            >
              + New
            </button>
            {/* Archive current portfolio — only when one is loaded from backend */}
            {portfolio.persistedId && (
              <button
                className={styles.deleteBtn}
                type="button"
                onClick={() => setArchiveTarget(portfolio.persistedId)}
                title="Archive portfolio"
                aria-label="Archive portfolio"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" width="14" height="14" aria-hidden="true">
                  <polyline points="3 6 5 6 21 6" />
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                </svg>
              </button>
            )}
          </div>
          <div className={styles.headerActions}>
            {/* Named-save input (portfolio-specific — the name drives load). */}
            <div className={styles.saveGroup}>
              <input
                className={styles.saveInput}
                type="text"
                value={saveInput}
                onChange={(e) => setSaveInput(e.target.value)}
                placeholder="Portfolio name"
                onKeyDown={(e) => { if (e.key === 'Enter') handleSave(); }}
              />
            </div>
            {/* Shared Save button + Auto save checkbox. */}
            <SaveControls
              dirty={
                !portfolio.persistedLocked && (
                  portfolio.dirty
                  || (saveInput.trim() !== '' && saveInput.trim() !== portfolio.portfolioName)
                )
              }
              autosave={portfolio.autosave}
              onSave={handleSave}
              onToggleAutosave={portfolio.setAutosave}
              saveDisabled={
                portfolio.persistedLocked
                || (!saveInput.trim() && !portfolio.portfolioName)
                || portfolio.legs.length === 0
              }
            />
            {/* Backend autosave status — shown when editing a persisted
                portfolio OR when a one-shot operation has a pending result.
                One-shot status takes priority; falls back to debounce status. */}
            {(oneshotStatus !== 'idle' || portfolio.persistedId) && (
              <SaveStatus
                status={displayedSaveStatus}
                label="Cloud"
                errorMessage={
                  displayedSaveStatus === 'error' ? saveErrorMessage : null
                }
              />
            )}
          </div>
        </div>

        {/* ── Error banner ── */}
        {portfolio.error && (
          <div className={styles.errorBanner}>
            <span>{portfolio.error}</span>
            <button
              className={styles.errorDismiss}
              type="button"
              onClick={portfolio.clearError}
              aria-label="Dismiss error"
            >
              &#215;
            </button>
          </div>
        )}

        {/* ── Lock banner — shown when the loaded portfolio is locked ── */}
        {portfolio.persistedLocked && (
          <LockBanner
            entityLabel="portfolio"
            className={styles.lockBanner}
            testId="portfolio-lock-banner"
          />
        )}

        {/* ── Saved portfolios panel ── */}
        <div className={styles.section}>
          {fetchError && (
            <div className={styles.error} data-testid="portfolio-fetch-error">
              {fetchError}
            </div>
          )}
          <PersistedPortfolioPanel
            category={portfolio.persistedCategory}
            onCategoryChange={portfolio.setPersistedCategory}
            portfolios={portfolios}
            loading={portfoliosLoading}
            onSaveCurrent={handleCreatePortfolio}
            saveDisabled={portfolio.legs.length === 0}
            onChangeItemCat={handleChangePortfolioCat}
            onArchive={handleArchivePortfolio}
            selectedId={portfolio.persistedId}
            onSelect={handleSelectPersisted}
            onSetPortfolioLocked={handleSetPortfolioLocked}
          />
        </div>

        {/* ── Holdings section ── */}
        {/* When the loaded portfolio is locked, the native disabled <fieldset>
            makes every holdings control non-interactive (mirrors the
            Indicators editor's read-only definition). Compute and the view
            slider stay enabled below so a locked portfolio is still
            inspectable — loading a persisted portfolio does not auto-compute,
            so the user must Compute to view it. The unlock control lives in
            the saved-portfolios list, so this never traps the user. */}
        <fieldset
          className={`${styles.section} ${styles.editorFieldset}`}
          disabled={portfolio.persistedLocked}
          data-testid="portfolio-editor-fieldset"
        >
          <HoldingsList
            legs={portfolio.legs}
            legDateRanges={portfolio.legDateRanges}
            onUpdateLeg={portfolio.updateLeg}
            onRemoveLeg={portfolio.removeLeg}
            onOpenAddModal={handleOpenModal}
            onOpenSignalModal={() => setSignalModalOpen(true)}
            onEditLeg={handleEditLeg}
            readOnly={portfolio.persistedLocked}
          />
        </fieldset>

        {/* ── Configuration bar ── */}
        <div className={`${styles.section} ${styles.configBar}`}>
          <div className={styles.configRow}>
            {/* Rebalance frequency — part of the saved portfolio definition,
                so it is disabled (via the native fieldset) when locked.
                Compute stays outside this fieldset and remains enabled so a
                locked portfolio can still be computed and inspected. */}
            <fieldset
              className={`${styles.configItem} ${styles.editorFieldset}`}
              disabled={portfolio.persistedLocked}
              title="Periodically reset allocations to target weights. Without rebalancing, positions drift as prices move."
            >
              <label className={styles.configLabel} htmlFor="rebalance-select">
                Rebalance
              </label>
              <select
                id="rebalance-select"
                className={styles.select}
                value={portfolio.rebalance}
                onChange={(e) => portfolio.setRebalance(e.target.value)}
              >
                {REBALANCE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </fieldset>

            {/* Compute button */}
            <button
              className={styles.computeBtn}
              type="button"
              onClick={portfolio.handleCalculate}
              disabled={portfolio.legs.length === 0 || portfolio.loading}
            >
              {portfolio.loading ? 'Computing...' : 'Compute'}
            </button>
          </div>

          {/* Time range slider */}
          <div className={styles.sliderRow}>
            <TimeRangeSlider
              minDate={portfolio.overlapRange?.start || null}
              maxDate={portfolio.overlapRange?.end || null}
              startDate={portfolio.startDate}
              endDate={portfolio.endDate}
              disabled={portfolio.loading || portfolio.rangesLoading}
              onChange={({ startDate, endDate }) => {
                portfolio.setStartDate(startDate);
                portfolio.setEndDate(endDate);
              }}
            />
          </div>
        </div>

        {/* ── Loading indicator ── */}
        {portfolio.loading && (
          <div className={styles.section}>
            <div className={styles.loadingBar}>
              <div className={styles.loadingBarFill} />
            </div>
          </div>
        )}

        {/* ── Results ── */}
        {portfolio.results && (
          <div className={styles.results}>
            {/* Date range info */}
            {portfolio.results.date_range && (
              <div className={styles.dateRangeInfo}>
                Data range: {portfolio.results.date_range.start} to {portfolio.results.date_range.end}
              </div>
            )}

            {/* Equity chart */}
            <div className={styles.section}>
              <PortfolioEquityChart
                dates={portfolio.results.dates}
                portfolioEquity={portfolio.results.portfolio_equity}
                legEquities={portfolio.results.leg_equities}
                rawLegEquities={portfolio.results.raw_leg_equities}
                rebalanceDates={portfolio.results.rebalance_dates}
                legs={portfolio.legs}
              />
            </div>

            {/* Statistics — needs an integer-YYYYMMDD date array and the
                portfolio equity curve. Statistics' internal inputsKey
                only tracks length/endpoints, so we force a remount via
                ``key`` whenever the rebalance frequency or holdings
                identity change — those produce same-length, same-endpoint
                curves with different middle values. */}
            {(() => {
              const statDates = isoDatesToYYYYMMDD(portfolio.results.dates);
              const statEquity = portfolio.results.portfolio_equity;
              if (!statDates || !Array.isArray(statEquity) || statEquity.length < 2) {
                return null;
              }
              // Backend rejects non-finite or non-positive equity — skip
              // mounting Statistics rather than surfacing a 400 inside it.
              if (statEquity.some((v) => !Number.isFinite(v) || v <= 0)) {
                return null;
              }
              const legsSig = portfolio.legs
                .map((l) => `${l.label}:${l.weight}`)
                .join('|');
              const remountKey = `${portfolio.results.rebalance || 'none'}|${statDates.length}|${legsSig}`;
              return (
                <div className={styles.section}>
                  <Statistics
                    key={remountKey}
                    dates={statDates}
                    equity={statEquity}
                    defaultRiskFreeRate={getRiskFreeRateFraction()}
                  />
                </div>
              );
            })()}

            {/* Returns grid */}
            <div className={styles.section}>
              <ReturnsGrid
                monthlyReturns={portfolio.results.monthly_returns}
                yearlyReturns={portfolio.results.yearly_returns}
              />
            </div>

            {/* Trade log — Portfolio response emits dates as ISO strings
                but TradeLog expects unix-ms timestamps; convert at the call
                site. entry/exitDescriptions are a union across all signal
                legs' specs; legs that lack a loaded spec contribute nothing
                (TradeLog falls back to the block name). */}
            {(() => {
              const trades = Array.isArray(portfolio.results.trades)
                ? portfolio.results.trades
                : [];
              const positions = Array.isArray(portfolio.results.positions)
                ? portfolio.results.positions
                : [];
              const timestamps = Array.isArray(portfolio.results.dates)
                ? portfolio.results.dates.map((d) => new Date(d).getTime())
                : [];
              const signalLegs = portfolio.legs.filter((l) => l.type === 'signal');
              const entryDescriptions = {};
              const exitDescriptions = {};
              for (const leg of signalLegs) {
                const entries = leg.signalSpec?.rules?.entries;
                if (Array.isArray(entries)) {
                  for (const b of entries) {
                    if (b && b.id) {
                      entryDescriptions[b.id] = typeof b.description === 'string' ? b.description : '';
                    }
                  }
                }
                const exits = leg.signalSpec?.rules?.exits;
                if (Array.isArray(exits)) {
                  for (const b of exits) {
                    if (b && b.id) {
                      exitDescriptions[b.id] = typeof b.description === 'string' ? b.description : '';
                    }
                  }
                }
              }
              // Roll rows (rolling direct legs) carry their own hover text
              // ("rolling <input name>") on the trade; surface it through the
              // same descriptions channel, keyed by the row's roll:<label> id.
              for (const tr of trades) {
                if (typeof tr.roll_hover === 'string' && tr.roll_hover) {
                  if (tr.entry_block_id) entryDescriptions[tr.entry_block_id] = tr.roll_hover;
                  if (tr.exit_block_id) exitDescriptions[tr.exit_block_id] = tr.roll_hover;
                }
              }
              return (
                <div className={styles.section}>
                  <TradeLog
                    trades={trades}
                    timestamps={timestamps}
                    positions={positions}
                    entryDescriptions={entryDescriptions}
                    exitDescriptions={exitDescriptions}
                    showHoldingColumn
                  />
                </div>
              );
            })()}
          </div>
        )}

        {/* ── Empty state ── */}
        {!portfolio.results && !portfolio.loading && portfolio.legs.length === 0 && (
          <div className={styles.emptyState}>
            <div className={styles.emptyIcon}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" width="28" height="28" aria-hidden="true">
                <path d="M21.21 15.89A10 10 0 1 1 8 2.83" />
                <path d="M22 12A10 10 0 0 0 12 2v10z" />
              </svg>
            </div>
            <span className={styles.emptyTitle}>Build your portfolio</span>
            <span className={styles.emptyHint}>
              Add instruments, set weights and date range, then compute performance.
            </span>
          </div>
        )}
      </div>

      {/* ── Add / Edit Holding Modal (single shared instance) ──
          Rendered OUTSIDE the locked <fieldset> so its controls are never
          disabled by the fieldset. editLegIndex non-null => edit mode; the
          modal pre-fills from the leg and updates it in place on confirm. */}
      <AddHoldingModal
        isOpen={modalOpen || editLegIndex !== null}
        onClose={handleCloseModal}
        onAddLeg={portfolio.addLeg}
        editLeg={editLegIndex !== null ? portfolio.legs[editLegIndex] : null}
        onUpdateLeg={(updates) => {
          if (editLegIndex !== null) portfolio.updateLeg(editLegIndex, updates);
        }}
        readOnly={portfolio.persistedLocked}
        referenceDate={portfolio.startDate}
      />

      {/* ── Add Signal Modal ── */}
      <SignalPickerModal
        isOpen={signalModalOpen}
        onClose={() => setSignalModalOpen(false)}
        onSelect={(signal) => {
          portfolio.addSignalLeg(signal);
          setSignalModalOpen(false);
        }}
      />

      {/* ── Archive portfolio confirmation ── */}
      <ConfirmDialog
        open={archiveTarget !== null}
        title="Archive portfolio?"
        message={
          archiveTarget
            ? `The portfolio "${
                portfolios.find((p) => p.id === archiveTarget)?.name
                || portfolio.portfolioName
                || archiveTarget
              }" will be moved to the Archive category.`
            : ''
        }
        confirmLabel="Archive"
        cancelLabel="Cancel"
        destructive
        onConfirm={() => {
          const id = archiveTarget;
          setArchiveTarget(null);
          if (id) handleArchivePortfolio(id);
        }}
        onCancel={() => setArchiveTarget(null)}
      />

      {/* ── New portfolio confirmation ── */}
      <ConfirmDialog
        open={clearConfirmOpen}
        title="Start a new portfolio?"
        message="Current holdings and results will be cleared. Saved portfolios are not affected."
        confirmLabel="New portfolio"
        cancelLabel="Cancel"
        destructive
        onConfirm={() => {
          setClearConfirmOpen(false);
          portfolio.clearAll();
        }}
        onCancel={() => setClearConfirmOpen(false)}
      />
    </div>
  );
}

export default PortfolioPage;
