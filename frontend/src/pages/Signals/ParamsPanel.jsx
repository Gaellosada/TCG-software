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
function ParamsPanel({ signal, onRun, running, canRun, runDisabledReason, capital, onCapitalChange, noRepeat, onNoRepeatChange }) {
  const rules = signal?.rules || {};
  const counts = {
    long_entry: (rules.long_entry || []).length,
    long_exit: (rules.long_exit || []).length,
    short_entry: (rules.short_entry || []).length,
    short_exit: (rules.short_exit || []).length,
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
            <li><span>Long entry blocks</span><strong>{counts.long_entry}</strong></li>
            <li><span>Long exit blocks</span><strong>{counts.long_exit}</strong></li>
            <li><span>Short entry blocks</span><strong>{counts.short_entry}</strong></li>
            <li><span>Short exit blocks</span><strong>{counts.short_exit}</strong></li>
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
        <label className={styles.noRepeatRow}>
          <input
            type="checkbox"
            checked={noRepeat}
            onChange={(e) => onNoRepeatChange(e.target.checked)}
            data-testid="no-repeat-checkbox"
          />
          <span className={styles.noRepeatLabel}>Don&apos;t repeat entries/exits</span>
          <span
            className={styles.noRepeatInfo}
            title="When checked, only show effective entries and exits — consecutive duplicate markers are hidden. Computation is unchanged."
          >
            &#9432;
          </span>
        </label>
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
