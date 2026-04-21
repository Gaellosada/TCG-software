import { useMemo, useState } from 'react';
import Chart from '../../components/Chart';
import styles from './Signals.module.css';
import {
  buildResultsPlot,
  buildClipSummary,
} from './resultsPlotTraces';

/**
 * Results section — unified subplot chart.
 *
 * A SINGLE <Chart> with Plotly domain-based subplots (stacked vertically,
 * shared x-axis):
 *   - Top subplot:    prices + P&L + capital (aggregated).
 *   - Bottom subplot: prices + overlay indicators + entry/exit markers.
 *   - Additional subplots: one per ownPanel indicator.
 *
 * The grid row height is driven by SignalsPage via the CSS variable
 * ``--results-row-min``, which grows when ownPanel indicators are present.
 * This component fills that space via ``flex: 1``.
 *
 * Loading / empty / error / clip-banner states are owned by this shell
 * and shown ABOVE the chart.
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

function ResultsView({ result, loading, error, capital = 1000, noRepeat = false }) {
  const plot = useMemo(() => buildResultsPlot(result, { capital, noRepeat }), [result, capital, noRepeat]);
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

  if (!plot.hasData) {
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
      <div
        className={styles.resultsPlotUnified}
        data-testid="results-plot-unified"
      >
        <Chart
          traces={plot.traces}
          layoutOverrides={plot.layoutOverrides}
          className={styles.chart}
          style={{ width: '100%', height: '100%' }}
          downloadFilename="signal-results"
        />
      </div>
    </div>
  );
}

export default ResultsView;
