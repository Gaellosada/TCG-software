import { useMemo, useState } from 'react';
import Chart from '../../components/Chart';
import { TRACE_COLORS } from '../../utils/chartTheme';
import styles from './IndicatorChart.module.css';

/**
 * Bottom panel — either a Plotly chart or a styled error card.
 *
 * Props:
 *   indicator {Object|null}   current selected indicator (used for title)
 *   result    {Object|null}   backend compute response
 *   loading   {boolean}       request in-flight
 *   error     {Object|null}   { error_type, message, traceback? }
 *                             — rendered in place of the chart when present
 */
function IndicatorChart({ indicator, result, loading, error }) {
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

    const indTrace = {
      x: dates,
      y: result.indicator,
      type: 'scatter',
      mode: 'lines',
      name: indicator?.name || 'Indicator',
      line: { color: '#f59e0b', width: 1.5 },
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      connectgaps: false,
      ...(useY2 ? { yaxis: 'y2' } : {}),
    };

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

    return { traces: [...seriesTraces, indTrace], layoutOverrides: lo, hasData: true };
  }, [result, indicator]);

  if (loading) {
    return (
      <div className={styles.panel}>
        <div className={styles.state}>Computing...</div>
      </div>
    );
  }

  // Error takes precedence over empty state (but not over loading).
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

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>{indicator?.name || 'Indicator'}</span>
      </div>
      <div className={styles.chartWrap}>
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

const HEADINGS = {
  validation: 'Validation error',
  runtime: 'Runtime error in your code',
  data: 'Data error',
};

function ErrorCard({ error }) {
  const kind = HEADINGS[error.error_type] ? error.error_type : 'generic';
  const heading = HEADINGS[kind] || 'Error running indicator';
  const iconPath = {
    validation: 'M12 9v4M12 17h.01M4 19h16a2 2 0 0 0 1.7-3L13.7 4a2 2 0 0 0-3.4 0L2.3 16A2 2 0 0 0 4 19z',
    runtime: 'M12 9v4M12 17h.01M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0z',
    data: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 8v4l3 2',
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
