import { useMemo } from 'react';
import Chart from '../../components/Chart';
import ErrorCard from '../../components/ErrorCard/ErrorCard';
import Statistics from '../../components/Statistics';
import styles from './Signals.module.css';
import { aggregateRealizedPnl, buildResultsPlot } from './resultsPlotTraces';
import { computeEffectiveTrace } from './runGate';

// Signals returns ``timestamps`` as unix-ms integers (see signals.py
// ``_int_yyyymmdd_to_unix_ms``). Statistics expects YYYYMMDD ints, so
// convert at the call site rather than bending the shared contract.
function unixMsToYYYYMMDD(timestamps) {
  if (!Array.isArray(timestamps)) return null;
  const out = new Array(timestamps.length);
  for (let i = 0; i < timestamps.length; i++) {
    const ms = timestamps[i];
    if (!Number.isFinite(ms)) return null;
    const d = new Date(ms);
    // UTC components — timestamps were produced from UTC midnight on the
    // backend, so reading local-time components would slip days in
    // negative-offset zones.
    const y = d.getUTCFullYear();
    const m = d.getUTCMonth() + 1;
    const day = d.getUTCDate();
    out[i] = y * 10000 + m * 100 + day;
  }
  return out;
}

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

function ResultsView({ result, loading, error, capital = 1000, noRepeat = false, signalRules = null, signalId = null }) {
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

  // Derive the equity curve for Statistics. Same construction as the
  // top subplot's "capital" trace: aggregated realized P&L summed across
  // inputs, scaled by capital, then added to capital. We compute it
  // unconditionally (cheap) — the consumers below decide whether to
  // render it.
  const statsInputs = useMemo(() => {
    if (!result || !Array.isArray(result.timestamps)) return null;
    const dates = unixMsToYYYYMMDD(result.timestamps);
    if (!dates || dates.length < 2) return null;
    const pnlRaw = aggregateRealizedPnl(result.realized_pnl, result.timestamps.length);
    if (!pnlRaw) return null;
    const cap = Number.isFinite(capital) ? capital : 1;
    const equity = pnlRaw.map((v) => cap + v * cap);
    return { dates, equity };
  }, [result, capital]);

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

  // Statistics' inputsKey only tracks length/endpoints, but a same-length
  // run with a tweaked rule could produce a same-endpoint curve with
  // different middle values. Force-remount the panel whenever the signal
  // id or capital changes so the metrics always reflect the latest run.
  const statsKey = statsInputs
    ? `${signalId ?? 'signal'}|${capital}|${statsInputs.dates.length}|${statsInputs.dates[0]}|${statsInputs.dates[statsInputs.dates.length - 1]}`
    : null;

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
      {statsInputs && (
        <div data-testid="signal-statistics">
          <Statistics
            key={statsKey}
            dates={statsInputs.dates}
            equity={statsInputs.equity}
          />
        </div>
      )}
    </div>
  );
}

export default ResultsView;
