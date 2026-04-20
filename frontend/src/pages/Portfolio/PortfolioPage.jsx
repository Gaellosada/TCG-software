import { useState, useCallback, useEffect } from 'react';
import usePortfolio from './usePortfolio';
import HoldingsList from './HoldingsList';
import AddHoldingModal from './AddHoldingModal';
import TimeRangeSlider from '../../components/TimeRangeSlider';
import PortfolioEquityChart from './PortfolioEquityChart';
import ReturnsGrid from './ReturnsGrid';
import SaveControls from '../../components/SaveControls';
import ConfirmDialog from '../../components/ConfirmDialog';
import styles from './PortfolioPage.module.css';

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
  const [saveInput, setSaveInput] = useState('');
  const [savedList, setSavedList] = useState(() => portfolio.getSavedPortfolios());
  // iter-4: replaced window.confirm with shared ConfirmDialog.
  // deleteTarget holds the portfolioName pending-delete (null = dialog closed).
  // clearConfirmOpen gates the clear-all dialog (no payload needed).
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);

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

        {/* ── Holdings section ── */}
        <div className={styles.section}>
          <HoldingsList
            legs={portfolio.legs}
            legDateRanges={portfolio.legDateRanges}
            onUpdateLeg={portfolio.updateLeg}
            onRemoveLeg={portfolio.removeLeg}
            onOpenAddModal={handleOpenModal}
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
              minDate={portfolio.results?.full_date_range?.start || portfolio.overlapRange?.start || null}
              maxDate={portfolio.results?.full_date_range?.end || portfolio.overlapRange?.end || null}
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

            {/* Returns grid */}
            <div className={styles.section}>
              <ReturnsGrid
                monthlyReturns={portfolio.results.monthly_returns}
                yearlyReturns={portfolio.results.yearly_returns}
              />
            </div>
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
