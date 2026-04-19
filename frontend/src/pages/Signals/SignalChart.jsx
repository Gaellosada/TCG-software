import { useMemo, useState } from 'react';
import Chart from '../../components/Chart';
import { TRACE_COLORS } from '../../utils/chartTheme';
import styles from './Signals.module.css';

/**
 * Bottom panel — stacked Plotly chart with:
 *   - top pane (yaxis)  : price of the FIRST instrument operand walked
 *                         in stable order (long_entry → long_exit →
 *                         short_entry → short_exit; block index; cond
 *                         index; lhs before rhs). Entry/exit markers are
 *                         plotted on this price line. If no instrument
 *                         operand is present anywhere in the spec, the
 *                         top pane is hidden and only the position
 *                         subplot renders.
 *   - bottom pane (yaxis2): ``position`` series (∈ [-1, +1]) from the
 *                         backend response.
 *
 * We don't fetch the price ourselves — the backend-response already
 * includes the price series for the first instrument operand it
 * encountered (same walk order). This component just knows how to
 * render it.
 *
 * Props:
 *   result   {Object|null}  shape: { index, position, long_score, short_score,
 *                                    entries_long, exits_long,
 *                                    entries_short, exits_short,
 *                                    price?: {label, values} }
 *   loading  {boolean}
 *   error    {Object|null}  { error_type, message, traceback? }
 */
const ERROR_HEADINGS = {
  validation: 'Invalid signal',
  runtime: 'Signal error',
  data: 'Data error',
  network: "Couldn't reach the server",
  offline: "You're offline",
};

function SignalChart({ result, loading, error }) {
  const { traces, layoutOverrides, hasData, hasPrice } = useMemo(() => {
    if (!result || !Array.isArray(result.index) || result.index.length === 0) {
      return { traces: [], layoutOverrides: {}, hasData: false, hasPrice: false };
    }
    const dates = result.index;
    const position = Array.isArray(result.position) ? result.position : [];
    const entriesLong = Array.isArray(result.entries_long) ? result.entries_long : [];
    const exitsLong = Array.isArray(result.exits_long) ? result.exits_long : [];
    const entriesShort = Array.isArray(result.entries_short) ? result.entries_short : [];
    const exitsShort = Array.isArray(result.exits_short) ? result.exits_short : [];

    // Price series is optional; when absent we render position-only.
    const priceValues = (result.price && Array.isArray(result.price.values))
      ? result.price.values
      : null;
    const priceLabel = (result.price && typeof result.price.label === 'string')
      ? result.price.label
      : 'Price';
    const hasPrice = priceValues !== null;

    const priceTraces = [];
    if (hasPrice) {
      priceTraces.push({
        x: dates,
        y: priceValues,
        type: 'scatter',
        mode: 'lines',
        name: priceLabel,
        yaxis: 'y',
        line: { color: TRACE_COLORS[0] || '#2563eb', width: 1 },
        hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
        connectgaps: false,
      });

      function markerTrace(indices, name, color, symbol) {
        const xs = [];
        const ys = [];
        for (const i of indices) {
          if (i >= 0 && i < dates.length) {
            xs.push(dates[i]);
            ys.push(priceValues[i]);
          }
        }
        return {
          x: xs,
          y: ys,
          type: 'scatter',
          mode: 'markers',
          name,
          yaxis: 'y',
          marker: { color, symbol, size: 10, line: { width: 1, color: '#ffffff' } },
          hovertemplate: '%{x}<br>%{y:,.4f}<extra>' + name + '</extra>',
        };
      }

      priceTraces.push(markerTrace(entriesLong,  'Long entry',  '#10b981', 'triangle-up'));
      priceTraces.push(markerTrace(exitsLong,    'Long exit',   '#059669', 'triangle-down'));
      priceTraces.push(markerTrace(entriesShort, 'Short entry', '#ef4444', 'triangle-down'));
      priceTraces.push(markerTrace(exitsShort,   'Short exit',  '#dc2626', 'triangle-up'));
    }

    const positionTrace = {
      x: dates,
      y: position,
      type: 'scatter',
      mode: 'lines',
      name: 'Position',
      yaxis: hasPrice ? 'y2' : 'y',
      line: { color: '#f59e0b', width: 1.5 },
      fill: 'tozeroy',
      fillcolor: 'rgba(245, 158, 11, 0.15)',
      hovertemplate: '%{x}<br>%{y:.3f}<extra></extra>',
      connectgaps: false,
    };

    let lo;
    if (hasPrice) {
      lo = {
        xaxis: { anchor: 'y2' },
        yaxis: {
          title: { text: 'Price', font: { size: 11 } },
          domain: [0.42, 1.0],
        },
        yaxis2: {
          title: { text: 'Position', font: { size: 11 } },
          domain: [0, 0.38],
          range: [-1.05, 1.05],
          anchor: 'x',
          zeroline: true,
        },
        showlegend: true,
      };
    } else {
      // No instrument operand — render the position subplot full-height
      // with a dashed zero line for context.
      lo = {
        yaxis: {
          title: { text: 'Position', font: { size: 11 } },
          range: [-1.05, 1.05],
          zeroline: true,
        },
        showlegend: true,
        legend: { orientation: 'h', y: -0.15 },
      };
    }
    return {
      traces: [...priceTraces, positionTrace],
      layoutOverrides: lo,
      hasData: true,
      hasPrice,
    };
  }, [result]);

  if (loading) {
    return (
      <div className={styles.chartPanelBody}>
        <div className={styles.chartState}>Computing...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.chartPanelBody}>
        <ErrorCard error={error} />
      </div>
    );
  }

  if (!hasData) {
    return (
      <div className={styles.chartPanelBody}>
        <div className={styles.chartState}>Run to see chart</div>
      </div>
    );
  }

  const testId = hasPrice ? 'signal-chart-full' : 'signal-chart-position-only';
  return (
    <div className={styles.chartPanelBody}>
      {!hasPrice && (
        <div
          className={styles.chartState}
          data-testid="signal-chart-subtitle"
        >
          No instrument operand in this signal — price overlay hidden.
        </div>
      )}
      <div className={styles.chartWrap} data-testid={testId}>
        <Chart
          traces={traces}
          layoutOverrides={layoutOverrides}
          className={styles.chart}
          downloadFilename="signal-result"
        />
      </div>
    </div>
  );
}

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

export default SignalChart;
