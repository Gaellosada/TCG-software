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
import ConfirmDialog from '../../components/ConfirmDialog';
import Statistics from '../../components/Statistics';
import TradeLog from '../../components/TradeLog';
import styles from './PortfolioPage.module.css';
import { getRiskFreeRateFraction } from '../../lib/userSettings';
import {
  listPortfolios,
  createPortfolio,
  updatePortfolio,
  archivePortfolio,
} from '../../api/persistence';

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
  const [signalModalOpen, setSignalModalOpen] = useState(false);
  const [saveInput, setSaveInput] = useState('');
  const [savedList, setSavedList] = useState(() => portfolio.getSavedPortfolios());
  // iter-4: replaced window.confirm with shared ConfirmDialog.
  // deleteTarget holds the portfolioName pending-delete (null = dialog closed).
  // clearConfirmOpen gates the clear-all dialog (no payload needed).
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);

  // --- Persisted portfolio panel -------------------------------------------
  const [persistedCategory, setPersistedCategory] = useState('RESEARCH');
  const [persistedPortfolios, setPersistedPortfolios] = useState([]);
  const [persistedLoading, setPersistedLoading] = useState(false);

  // Separate status state for one-shot operations (save-current / archive /
  // category-change). Kept separate from the debounced autosave status so
  // neither path's timing can overwrite the other.
  const [oneshotStatus, setOneshotStatus] = useState('idle');

  const fetchPersistedPortfolios = useCallback(async (cat) => {
    setPersistedLoading(true);
    try {
      const docs = await listPortfolios(cat);
      setPersistedPortfolios(docs);
    } catch {
      setPersistedPortfolios([]);
    } finally {
      setPersistedLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchPersistedPortfolios(persistedCategory);
  }, [persistedCategory, fetchPersistedPortfolios]);

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
  })), []);

  // Save current portfolio state to backend in the selected category.
  // After a successful create, takes over autosave by setting
  // ``persistedId`` so ongoing edits get debounced PUTs.
  const handlePersistSave = useCallback(async () => {
    const name = saveInput.trim() || portfolio.portfolioName || 'Portfolio';
    const id = `portfolio-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    setOneshotStatus('saving');
    try {
      await createPortfolio({
        id,
        name,
        category: persistedCategory,
        legs: legsToWire(portfolio.legs),
        rebalance: portfolio.rebalance || 'none',
      });
      setOneshotStatus('saved');
      portfolio.setPersistedId(id);
      // Make sure the portfolioName state reflects what we just saved
      // so subsequent autosaves include it.
      if (name !== portfolio.portfolioName) {
        portfolio.setPortfolioName(name);
      }
      fetchPersistedPortfolios(persistedCategory);
    } catch {
      setOneshotStatus('error');
    }
  }, [saveInput, portfolio, persistedCategory, fetchPersistedPortfolios, legsToWire]);

  // Move a persisted portfolio to a different category. Preserves all
  // editable content via the full-replace PUT.
  const handleChangePortfolioCat = useCallback(async (id, newCat) => {
    const target = persistedPortfolios.find((p) => p.id === id);
    if (!target) return;
    setOneshotStatus('saving');
    try {
      await updatePortfolio(id, {
        name: target.name,
        category: newCat,
        legs: target.legs || [],
        rebalance: target.rebalance || 'none',
      });
      setOneshotStatus('saved');
      fetchPersistedPortfolios(persistedCategory);
    } catch {
      setOneshotStatus('error');
    }
  }, [persistedPortfolios, persistedCategory, fetchPersistedPortfolios]);

  // Archive (soft-delete) a persisted portfolio.
  const handleArchivePortfolio = useCallback(async (id) => {
    setOneshotStatus('saving');
    try {
      await archivePortfolio(id);
      setOneshotStatus('saved');
      // If we were editing this exact portfolio, drop the persistedId
      // so further edits don't try to autosave to an archived row.
      if (portfolio.persistedId === id) {
        portfolio.setPersistedId(null);
      }
      fetchPersistedPortfolios(persistedCategory);
    } catch {
      setOneshotStatus('error');
    }
  }, [persistedCategory, fetchPersistedPortfolios, portfolio]);

  // Load a backend-persisted portfolio into the editor.
  const handleSelectPersisted = useCallback((id) => {
    const doc = persistedPortfolios.find((p) => p.id === id);
    if (!doc) return;
    portfolio.loadFromPersisted(doc);
    setSaveInput(doc.name || '');
  }, [persistedPortfolios, portfolio]);

  // --- Backend debounced auto-save for the loaded persisted portfolio -----
  const cloudPayload = useMemo(() => {
    if (!portfolio.persistedId) return null;
    const persisted = persistedPortfolios.find((p) => p.id === portfolio.persistedId);
    if (!persisted) return null; // safety — list not yet loaded
    return JSON.stringify({
      name: portfolio.portfolioName || persisted.name || 'Portfolio',
      category: persisted.category,
      legs: legsToWire(portfolio.legs),
      rebalance: portfolio.rebalance || 'none',
    });
  }, [
    portfolio.persistedId,
    portfolio.portfolioName,
    portfolio.legs,
    portfolio.rebalance,
    persistedPortfolios,
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
    const persisted = persistedPortfolios.find((p) => p.id === portfolio.persistedId);
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
  }, [portfolio.persistedId, persistedPortfolios]);

  const cloudDirty = !!cloudPayload
    && (lastSeenPayloadRef.current.id !== portfolio.persistedId
        || lastSeenPayloadRef.current.payload !== cloudPayload);

  const handleCloudSave = useCallback(async (payloadStr) => {
    if (!portfolio.persistedId || !payloadStr) return;
    const body = JSON.parse(payloadStr);
    await updatePortfolio(portfolio.persistedId, body);
    lastSeenPayloadRef.current = {
      id: portfolio.persistedId,
      payload: payloadStr,
    };
    fetchPersistedPortfolios(persistedCategory);
  }, [portfolio.persistedId, persistedCategory, fetchPersistedPortfolios]);

  const {
    status: cloudStatus,
    reset: resetCloudStatus,
  } = useBackendAutosave({
    enabled: !!portfolio.persistedId && cloudDirty,
    payload: cloudPayload,
    onSave: handleCloudSave,
    debounceMs: 500,
  });

  // Reset cloud status indicator on portfolio (de)selection.
  useEffect(() => {
    resetCloudStatus();
  }, [portfolio.persistedId, resetCloudStatus]);

  // Pre-fill save input when a portfolio is loaded
  useEffect(() => {
    if (portfolio.portfolioName) {
      setSaveInput(portfolio.portfolioName);
    }
  }, [portfolio.portfolioName]);

  const handleOpenModal = useCallback(() => setModalOpen(true), []);
  const handleCloseModal = useCallback(() => setModalOpen(false), []);

  const refreshSavedList = useCallback(() => {
    setSavedList(portfolio.getSavedPortfolios());
  }, [portfolio.getSavedPortfolios]);

  const handleSave = useCallback(() => {
    // If editing a loaded portfolio and input is empty/unchanged, save with current name
    const name = saveInput.trim() || portfolio.portfolioName;
    if (!name) return;
    portfolio.savePortfolio(name);
    refreshSavedList();
  }, [saveInput, portfolio, refreshSavedList]);

  const handleLoad = useCallback(
    (name) => {
      portfolio.loadPortfolio(name);
      refreshSavedList();
    },
    [portfolio, refreshSavedList],
  );

  const handleDeleteSaved = useCallback(
    (name) => {
      portfolio.deleteSavedPortfolio(name);
      portfolio.clearAll();
      refreshSavedList();
    },
    [portfolio, refreshSavedList],
  );

  return (
    <div className={styles.page}>
      <div className={styles.scroll}>
        {/* ── Header ── */}
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            <h2 className={styles.pageTitle}>Portfolio</h2>
            {/* Load dropdown — right next to title */}
            {savedList.length > 0 && (
              <select
                className={styles.loadSelect}
                value=""
                onChange={(e) => {
                  if (e.target.value) handleLoad(e.target.value);
                }}
                aria-label="Load saved portfolio"
              >
                <option value="" disabled>
                  {portfolio.portfolioName || 'Load...'}
                </option>
                {savedList.map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
            )}
            {/* Delete current portfolio — only when one is loaded */}
            {portfolio.portfolioName && (
              <button
                className={styles.deleteBtn}
                type="button"
                onClick={() => setDeleteTarget(portfolio.portfolioName)}
                title="Delete saved portfolio"
                aria-label="Delete saved portfolio"
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
                portfolio.dirty
                || (saveInput.trim() !== '' && saveInput.trim() !== portfolio.portfolioName)
              }
              autosave={portfolio.autosave}
              onSave={handleSave}
              onToggleAutosave={portfolio.setAutosave}
              saveDisabled={
                (!saveInput.trim() && !portfolio.portfolioName)
                || portfolio.legs.length === 0
              }
            />
            {/* Backend autosave status — shown when editing a persisted
                portfolio OR when a one-shot operation has a pending result.
                One-shot status takes priority; falls back to debounce status. */}
            {(oneshotStatus !== 'idle' || portfolio.persistedId) && (
              <SaveStatus
                status={oneshotStatus !== 'idle' ? oneshotStatus : cloudStatus}
                label="Cloud"
              />
            )}
            {/* Clear — with confirmation */}
            <button
              className={styles.clearBtn}
              type="button"
              onClick={() => setClearConfirmOpen(true)}
              disabled={portfolio.legs.length === 0 && !portfolio.results}
            >
              Clear
            </button>
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

        {/* ── Persisted portfolios panel ── */}
        <PersistedPortfolioPanel
          category={persistedCategory}
          onCategoryChange={setPersistedCategory}
          portfolios={persistedPortfolios}
          loading={persistedLoading}
          onSaveCurrent={handlePersistSave}
          saveDisabled={portfolio.legs.length === 0}
          onChangeItemCat={handleChangePortfolioCat}
          onArchive={handleArchivePortfolio}
          selectedId={portfolio.persistedId}
          onSelect={handleSelectPersisted}
        />

        {/* ── Holdings section ── */}
        <div className={styles.section}>
          <HoldingsList
            legs={portfolio.legs}
            legDateRanges={portfolio.legDateRanges}
            onUpdateLeg={portfolio.updateLeg}
            onRemoveLeg={portfolio.removeLeg}
            onOpenAddModal={handleOpenModal}
            onOpenSignalModal={() => setSignalModalOpen(true)}
          />
        </div>

        {/* ── Configuration bar ── */}
        <div className={`${styles.section} ${styles.configBar}`}>
          <div className={styles.configRow}>
            {/* Rebalance frequency */}
            <div
              className={styles.configItem}
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
            </div>

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

      {/* ── Add Holding Modal ── */}
      <AddHoldingModal
        isOpen={modalOpen}
        onClose={handleCloseModal}
        onAddLeg={portfolio.addLeg}
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

      {/* ── Delete saved portfolio confirmation ── */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete saved portfolio?"
        message={
          deleteTarget
            ? `The saved portfolio "${deleteTarget}" will be permanently removed.`
            : ''
        }
        confirmLabel="Delete"
        cancelLabel="Cancel"
        destructive
        onConfirm={() => {
          const name = deleteTarget;
          setDeleteTarget(null);
          if (name) handleDeleteSaved(name);
        }}
        onCancel={() => setDeleteTarget(null)}
      />

      {/* ── Clear-all confirmation ── */}
      <ConfirmDialog
        open={clearConfirmOpen}
        title="Clear all holdings and results?"
        message="All holdings and computed results will be cleared. This cannot be undone."
        confirmLabel="Clear"
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
