import styles from './Signals.module.css';

/**
 * Right panel — Signal metadata + Run button. The Signals page has no
 * per-signal "parameters" the way Indicators do (the whole spec IS the
 * parameters), so this panel is intentionally spare: a summary box with
 * block counts per direction, a guarded Run button, and that's it.
 *
 * Props:
 *   signal             {Object|null}
 *   onRun              {Function}  () => void
 *   running            {boolean}
 *   canRun             {boolean}
 *   runDisabledReason  {string|null}
 */
function ParamsPanel({ signal, onRun, running, canRun, runDisabledReason }) {
  const rules = signal?.rules || {};
  const counts = {
    long_entry: (rules.long_entry || []).length,
    long_exit: (rules.long_exit || []).length,
    short_entry: (rules.short_entry || []).length,
    short_exit: (rules.short_exit || []).length,
  };

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
