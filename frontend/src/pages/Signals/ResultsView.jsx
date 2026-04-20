import { useMemo, useState } from 'react';
import Chart from '../../components/Chart';
import styles from './Signals.module.css';
import {
  buildTopPlot,
  buildBottomPlot,
  buildClipSummary,
} from './resultsPlotTraces';

/**
 * Results section — iter-5 ask #6.
 *
 * Two stacked Plotly charts in a vertical flex column:
 *   - Top plot: all declared inputs (price) + realized P&L (aggregated).
 *   - Bottom plot: inputs + indicators + entry/exit markers.
 *
 * Each plot is an independent ``<Chart>`` (its own legend, CSV export,
 * modebar). Loading / empty / error / clip-banner states are owned by
 * this shell and shown ABOVE the charts so both plots share the same
 * framing.
 */
const ERROR_HEADINGS = {
  validation: 'Invalid signal',
  runtime: 'Signal error',
  data: 'Data error',
  network: "Couldn't reach the server",
  offline: "You're offline",
};

function ErrorCard({ error }) {
  const heading = ERROR_HEADINGS[error.error_type] || 'Error running signal';
  const [copied, setCopied] = useState(false);
  function handleCopy() {
    const blob = error.traceback
      ? `${error.error_type}: ${error.message}\n\n${error.traceback}`
      : error.message;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(blob).then(
          () => { setCopied(true); setTimeout(() => setCopied(false), 1600); },
          () => { /* clipboard blocked */ },
        );
      }
    } catch { /* ignore */ }
  }
  return (
    <div className={styles.errorCard} data-error-type={error.error_type} role="alert">
      <div className={styles.errorHeader}>
        <h3 className={styles.errorHeading}>{heading}</h3>
        <button
          type="button"
          className={styles.copyBtn}
          onClick={handleCopy}
          aria-label="Copy error details"
        >
          {copied ? 'Copied!' : 'Copy'}
        </button>
      </div>
      <p className={styles.errorMessage}>{error.message}</p>
      {error.traceback && (
        <details className={styles.tracebackDetails}>
          <summary>Show traceback</summary>
          <pre className={styles.tracebackPre}>{error.traceback}</pre>
        </details>
      )}
    </div>
  );
}

function ResultsView({ result, loading, error }) {
  const top = useMemo(() => buildTopPlot(result), [result]);
  const bottom = useMemo(() => buildBottomPlot(result), [result]);
  const clipSummary = useMemo(() => buildClipSummary(result), [result]);

  if (loading) {
    return (
      <div className={styles.resultsViewBody}>
        <div className={styles.chartState}>Computing…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.resultsViewBody}>
        <ErrorCard error={error} />
      </div>
    );
  }

  if (!top.hasData && !bottom.hasData) {
    return (
      <div className={styles.resultsViewBody}>
        <div className={styles.chartState} data-testid="signal-chart-empty">
          Run a signal to see positions
        </div>
      </div>
    );
  }

  return (
    <div className={styles.resultsViewBody} data-testid="results-view">
      {clipSummary && clipSummary.rows.length > 0 && (
        <div
          className={styles.clipBanner}
          role="alert"
          data-testid="signal-chart-clip-banner"
        >
          <span className={styles.clipBannerIcon} aria-hidden="true">⚠</span>
          <span>
            <strong>Position clipped</strong> to [-1, +1] on{' '}
            {clipSummary.rows.map((r, i) => (
              <span key={r.instrument}>
                {i > 0 && ', '}
                <code>{r.instrument}</code> ({r.count} bar{r.count === 1 ? '' : 's'})
              </span>
            ))}
            . Raw long/short weight sums exceed 1.0 at those timestamps.
          </span>
        </div>
      )}
      <div className={styles.resultsPlotTop} data-testid="results-plot-top">
        <Chart
          traces={top.traces}
          layoutOverrides={top.layoutOverrides}
          className={styles.chart}
          downloadFilename="signal-inputs-pnl"
        />
      </div>
      <div className={styles.resultsPlotBottom} data-testid="results-plot-bottom">
        <Chart
          traces={bottom.traces}
          layoutOverrides={bottom.layoutOverrides}
          className={styles.chart}
          downloadFilename="signal-inputs-indicators-events"
        />
      </div>
    </div>
  );
}

export default ResultsView;
