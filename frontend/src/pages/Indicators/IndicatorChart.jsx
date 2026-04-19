import { useMemo, useState } from 'react';
import Chart from '../../components/Chart';
import { TRACE_COLORS } from '../../utils/chartTheme';
import { HEADINGS } from './errorTaxonomy';
import styles from './IndicatorChart.module.css';

/**
 * Bottom panel — one Plotly chart (overlay or stacked-subplot) or an
 * error card, depending on state.
 *
 * Layout modes (driven by ``indicator.ownPanel``):
 *   - ``ownPanel === false`` (default): price + indicator overlaid on a
 *     single y-axis. Retains the Y2 heuristic so small-magnitude indicators
 *     (RSI-ish things) get a secondary right-side axis instead of being
 *     squashed against large prices.
 *   - ``ownPanel === true``: stacked subplots inside a SINGLE Chart —
 *     price occupies the top ~half of the plot area (yaxis), indicator
 *     occupies the bottom ~half (yaxis2). Both share the same x-axis
 *     (via ``yaxis2.anchor = 'x'`` + ``xaxis.anchor = 'y2'``) so zoom/
 *     pan on one pane propagates to the other and hover is unified.
 *     Mirrors the Data page's price/volume stacked layout.
 */
function IndicatorChart({ indicator, result, loading, error }) {
  const ownPanel = !!indicator?.ownPanel;

  const { traces, layoutOverrides, hasData } = useMemo(() => {
    if (!result || !result.dates || result.dates.length === 0) {
      return { traces: [], layoutOverrides: {}, hasData: false };
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

    if (ownPanel) {
      // Stacked subplots in a single chart. Price top, indicator bottom;
      // x-axis anchored under the bottom pane so the shared axis ticks
      // sit below everything. 4% gap between panes keeps the divider
      // readable without wasting vertical space.
      const indTrace = { ...baseIndTrace, yaxis: 'y2' };
      const lo = {
        xaxis: { anchor: 'y2' },
        yaxis: {
          title: { text: 'Price', font: { size: 11 } },
          domain: [0.52, 1.0],
        },
        yaxis2: {
          title: { text: 'Indicator', font: { size: 11 } },
          domain: [0, 0.48],
          anchor: 'x',
        },
        showlegend: true,
      };
      return {
        traces: [...seriesTraces, indTrace],
        layoutOverrides: lo,
        hasData: true,
      };
    }

    // Overlay mode — retain the Y2 heuristic so small-magnitude
    // indicators get a right-side axis overlaying price.
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

    const overlayIndTrace = useY2 ? { ...baseIndTrace, yaxis: 'y2' } : baseIndTrace;
    const lo = {
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

    return {
      traces: [...seriesTraces, overlayIndTrace],
      layoutOverrides: lo,
      hasData: true,
    };
  }, [result, indicator?.name, ownPanel]);

  if (loading) {
    return (
      <div className={styles.panel}>
        <div className={styles.state}>Computing...</div>
      </div>
    );
  }

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
  const testId = ownPanel ? 'indicator-chart-split' : 'indicator-chart-overlay';
  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>{headerTitle}</span>
      </div>
      <div className={styles.chartWrap} data-testid={testId}>
        <Chart
          traces={traces}
          layoutOverrides={layoutOverrides}
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
