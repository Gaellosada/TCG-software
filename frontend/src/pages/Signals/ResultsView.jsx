import { useMemo } from 'react';
import Chart from '../../components/Chart';
import ErrorCard from '../../components/ErrorCard/ErrorCard';
import styles from './Signals.module.css';
import { buildResultsPlot } from './resultsPlotTraces';
import { computeEffectiveTrace } from './runGate';

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
 * Loading / empty / error states are owned by this shell and shown
 * ABOVE the chart.
 */

const ERROR_HEADINGS = {
  validation: 'Invalid signal',
  runtime: 'Signal error',
  data: 'Data error',
  network: "Couldn't reach the server",
  offline: "You're offline",
};

function ResultsView({ result, loading, error, capital = 1000, noRepeat = false, signalRules = null }) {
  // Effective-only display: when dont_repeat is active we rewrite each
  // event's ``fired_indices`` to its ``latched_indices`` (the
  // backend-authoritative effective set) via ``computeEffectiveTrace``,
  // then pass the trace to ``buildResultsPlot`` which renders markers
  // from ``fired_indices``. Downstream no longer needs to branch on the
  // flag — there is a single source of truth for "effective" semantics.
  const effectiveResult = useMemo(
    () => computeEffectiveTrace(result, { dontRepeat: noRepeat }),
    [result, noRepeat],
  );
  // signalRules is the v4 ``{entries, exits}`` rules object for the
  // currently-selected signal; buildResultsPlot uses it to resolve each
  // event's marker colour from the originating block's signed weight
  // (exits colour by their target-entry's weight). When omitted all
  // markers fall back to neutral styling.
  const plot = useMemo(
    () => buildResultsPlot(effectiveResult, { capital, signalRules }),
    [effectiveResult, capital, signalRules],
  );

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
        <ErrorCard
          error={error}
          headings={ERROR_HEADINGS}
          fallbackHeading="Error running signal"
          styles={styles}
        />
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
