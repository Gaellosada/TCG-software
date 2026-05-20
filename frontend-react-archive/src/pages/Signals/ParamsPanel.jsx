import styles from './Signals.module.css';

/**
 * Right panel — Signal metadata + Run button.
 *
 * Props:
 *   signal             {Object|null}
 *   onRun              {Function}  () => void
 *   running            {boolean}
 *   canRun             {boolean}
 *   runDisabledReason  {string|null}
 *   capital            {number}       display-only initial capital for P&L scaling
 *   onCapitalChange    {Function}     (number) => void
 */
function ParamsPanel({ signal, onRun, running, canRun, runDisabledReason, capital, onCapitalChange }) {
  // v4: rules shape is now `{ entries, exits }` (section model); weight sign
  // carries long/short on each entry block.
  const rules = signal?.rules || {};
  const counts = {
    entries: (rules.entries || []).length,
    exits: (rules.exits || []).length,
  };

  function handleCapitalChange(e) {
    const v = parseFloat(e.target.value);
    if (Number.isFinite(v) && v > 0) onCapitalChange(v);
  }

  return (
    <div className={styles.paramsPanelBody}>
      <div className={styles.paramsSection}>
        <div className={styles.paramsSectionLabel}>Summary</div>
        {!signal ? (
          <div className={styles.paramsPlaceholder}>No signal selected.</div>
        ) : (
          <ul className={styles.summaryList}>
            <li><span>Entry blocks</span><strong>{counts.entries}</strong></li>
            <li><span>Exit blocks</span><strong>{counts.exits}</strong></li>
          </ul>
        )}
      </div>
      <div className={styles.paramsDivider} />
      <div className={styles.paramsSection}>
        <div className={styles.paramsSectionLabel}>Run</div>
        <div className={styles.capitalRow}>
          <label className={styles.capitalLabel} htmlFor="initial-capital">Initial capital</label>
          <input
            id="initial-capital"
            type="number"
            min="1"
            step="100"
            className={styles.capitalInput}
            value={capital}
            onChange={handleCapitalChange}
            data-testid="initial-capital"
          />
        </div>
        <button
          type="button"
          className={styles.runBtn}
          onClick={onRun}
          disabled={!canRun}
          aria-label="Run signal"
          title={runDisabledReason || undefined}
          data-testid="run-signal-btn"
        >
          {running ? 'Computing...' : 'Run'}
        </button>
        {!canRun && runDisabledReason && (
          <div className={styles.runHint}>{runDisabledReason}</div>
        )}
      </div>
    </div>
  );
}

export default ParamsPanel;
