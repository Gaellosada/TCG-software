import { useMemo, useState } from 'react';
import Chart from '../../components/Chart';
import { TRACE_COLORS } from '../../utils/chartTheme';
import { HEADINGS } from './errorTaxonomy';
import styles from './IndicatorChart.module.css';

/**
 * Bottom panel — either one Plotly chart, two stacked charts, or an
 * error card, depending on state.
 *
 * Layout modes (driven by ``indicator.ownPanel``):
 *   - ``ownPanel === false`` (default): price + indicator overlaid on a
 *     single chart. Retains the Y2 heuristic so small-magnitude indicators
 *     (RSI-ish things) get a secondary axis instead of being squashed.
 *   - ``ownPanel === true``: two ``<Chart>`` components stacked vertically
 *     via a flex column — top carries the price traces only (no Y2),
 *     bottom carries the indicator trace only on its own natural scale.
 *     Avoids the "indicator is a flat line next to a $5k price" problem
 *     for indicators whose units genuinely differ from price (RSI, MACD,
 *     returns, etc.). Loading/error states are shared across both panes.
 *
 * Props:
 *   indicator {Object|null}   current selected indicator (used for title;
 *                             ``indicator.ownPanel`` drives the split)
 *   result    {Object|null}   backend compute response
 *   loading   {boolean}       request in-flight
 *   error     {Object|null}   { error_type, message, traceback? }
 *                             — rendered in place of the chart when present
 */
function IndicatorChart({ indicator, result, loading, error }) {
  const ownPanel = !!indicator?.ownPanel;

  const {
    combinedTraces,
    combinedLayout,
    priceTraces,
    priceLayout,
    indicatorTrace,
    indicatorLayout,
    hasData,
  } = useMemo(() => {
    const empty = {
      combinedTraces: [],
      combinedLayout: {},
      priceTraces: [],
      priceLayout: {},
      indicatorTrace: null,
      indicatorLayout: {},
      hasData: false,
    };
    if (!result || !result.dates || result.dates.length === 0) {
      return empty;
    }

    const dates = result.dates;
    const seriesTraces = (result.series || []).map((s, i) => ({
      x: dates,
      y: s.close,
      type: 'scatter',
      mode: 'lines',
      name: s.label || `${s.collection}/${s.instrument_id}`,
      line: { color: TRACE_COLORS[i % TRACE_COLORS.length], width: 1 },
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      connectgaps: false,
    }));

    // Y2 heuristic — only meaningful in overlay mode.
    const priceAbsMax = Math.max(
      0,
      ...((result.series || []).flatMap((s) =>
        (s.close || []).filter((v) => v !== null && Number.isFinite(v)).map((v) => Math.abs(v)),
      )),
    );
    const indAbsMax = Math.max(
      0,
      ...((result.indicator || [])
        .filter((v) => v !== null && Number.isFinite(v))
        .map((v) => Math.abs(v))),
    );
    const useY2 = indAbsMax < 10 && priceAbsMax > 100;

    const baseIndTrace = {
      x: dates,
      y: result.indicator,
      type: 'scatter',
      mode: 'lines',
      name: indicator?.name || 'Indicator',
      line: { color: '#f59e0b', width: 1.5 },
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      connectgaps: false,
    };

    // Overlay variant retains the Y2 tag when needed.
    const overlayIndTrace = useY2
      ? { ...baseIndTrace, yaxis: 'y2' }
      : baseIndTrace;

    const combinedLO = {
      yaxis: { title: { text: 'Price', font: { size: 11 } } },
      ...(useY2
        ? {
            yaxis2: {
              title: { text: 'Indicator', font: { size: 11 } },
              overlaying: 'y',
              side: 'right',
              showgrid: false,
            },
          }
        : {}),
      showlegend: true,
      legend: { orientation: 'h', y: -0.15 },
    };

    // Split layouts — each pane owns a single natural y-scale.
    const priceLO = {
      yaxis: { title: { text: 'Price', font: { size: 11 } } },
      showlegend: true,
      legend: { orientation: 'h', y: -0.15 },
    };
    const indicatorLO = {
      yaxis: { title: { text: 'Indicator', font: { size: 11 } } },
      showlegend: true,
      legend: { orientation: 'h', y: -0.15 },
    };

    return {
      combinedTraces: [...seriesTraces, overlayIndTrace],
      combinedLayout: combinedLO,
      priceTraces: seriesTraces,
      priceLayout: priceLO,
      indicatorTrace: baseIndTrace,
      indicatorLayout: indicatorLO,
      hasData: true,
    };
  }, [result, indicator?.name]);

  if (loading) {
    return (
      <div className={styles.panel}>
        <div className={styles.state}>Computing...</div>
      </div>
    );
  }

  // Error takes precedence over empty state (but not over loading). In
  // split mode, the error card still renders full-body — the compute
  // call is a single request so there's nothing meaningful to attribute
  // to "price pane" vs "indicator pane".
  if (error) {
    return (
      <div className={styles.panel}>
        <ErrorCard error={error} />
      </div>
    );
  }

  if (!hasData) {
    return (
      <div className={styles.panel}>
        <div className={styles.state}>
          {indicator ? 'Run to see chart' : 'No indicator selected'}
        </div>
      </div>
    );
  }

  const headerTitle = indicator?.name || 'Indicator';

  if (ownPanel) {
    // Stacked layout — equal-height flex children inside ``chartSplit``.
    const filename = `indicator-${indicator?.name || 'result'}`;
    return (
      <div className={styles.panel}>
        <div className={styles.header}>
          <span className={styles.title}>{headerTitle}</span>
        </div>
        <div className={styles.chartSplit} data-testid="indicator-chart-split">
          <div className={styles.chartWrapHalf}>
            <Chart
              traces={priceTraces}
              layoutOverrides={priceLayout}
              className={styles.chart}
              downloadFilename={`${filename}-price`}
            />
          </div>
          <div className={styles.chartWrapHalf}>
            <Chart
              traces={indicatorTrace ? [indicatorTrace] : []}
              layoutOverrides={indicatorLayout}
              className={styles.chart}
              downloadFilename={`${filename}-indicator`}
            />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>{headerTitle}</span>
      </div>
      <div className={styles.chartWrap} data-testid="indicator-chart-overlay">
        <Chart
          traces={combinedTraces}
          layoutOverrides={combinedLayout}
          className={styles.chart}
          downloadFilename={`indicator-${indicator?.name || 'result'}`}
        />
      </div>
    </div>
  );
}

function ErrorCard({ error }) {
  const kind = HEADINGS[error.error_type] ? error.error_type : 'generic';
  const heading = HEADINGS[kind] || 'Error running indicator';
  const iconPath = {
    validation: 'M12 9v4M12 17h.01M4 19h16a2 2 0 0 0 1.7-3L13.7 4a2 2 0 0 0-3.4 0L2.3 16A2 2 0 0 0 4 19z',
    runtime: 'M12 9v4M12 17h.01M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0z',
    data: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 8v4l3 2',
    network: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 8v4l3 2',
    offline: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 8v4l3 2',
    generic: 'M12 9v4M12 17h.01M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0z',
  }[kind];

  const [copied, setCopied] = useState(false);

  function handleCopy() {
    const blob = error.traceback
      ? `${error.error_type}: ${error.message}\n\n${error.traceback}`
      : error.message;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(blob).then(
          () => { setCopied(true); setTimeout(() => setCopied(false), 1600); },
          () => { /* clipboard blocked — swallow silently */ },
        );
      }
    } catch { /* ignore */ }
  }

  return (
    <div className={styles.errorCard} data-error-type={kind} role="alert">
      <div className={styles.errorHeader}>
        <svg
          viewBox="0 0 24 24"
          className={styles.errorIcon}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          width="22"
          height="22"
          aria-hidden="true"
        >
          <path d={iconPath} />
        </svg>
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

export default IndicatorChart;
