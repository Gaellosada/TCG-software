import { useMemo, useState } from 'react';
import Chart from '../../components/Chart';
import { TRACE_COLORS } from '../../utils/chartTheme';
import styles from './Signals.module.css';

/**
 * Bottom panel — v3 multi-input position chart (iter-4).
 *
 * Response shape (see PLAN.md § v3 contract):
 *   {
 *     timestamps: number[],   // unix ms
 *     positions: [
 *       {
 *         input_id: string,
 *         instrument: {type:'spot'|'continuous', ...},
 *         values: number[],         // ∈ [-1, +1]
 *         clipped_mask: boolean[],
 *         price: {label, values} | null,
 *       },
 *     ],
 *     indicators: IndicatorTrace[], // always a list (iter-3 PROB-1)
 *     clipped: boolean,             // global OR across all masks
 *   }
 *
 * Rendering:
 *   - One subplot per instrument, stacked vertically, shared x-axis.
 *   - Position series ∈ [-1, 1] on the left axis.
 *   - If ``price`` is present, overlay on a right y-axis within the
 *     same subplot.
 *   - If ``clipped === true``, show a red/amber warning banner above the
 *     chart listing affected instruments + bar count.
 */
const ERROR_HEADINGS = {
  validation: 'Invalid signal',
  runtime: 'Signal error',
  data: 'Data error',
  network: "Couldn't reach the server",
  offline: "You're offline",
};

/**
 * Build a human-readable label for a position. v3 positions carry
 * ``input_id`` plus a typed instrument; we combine them so multi-input
 * signals read as "X • INDEX:SPX" / "Y • cont FUT_ES".
 */
function formatInstrumentLabel(position) {
  const inst = position && position.instrument;
  const inputId = position && position.input_id;
  let instStr = '';
  if (inst && typeof inst === 'object') {
    if (inst.type === 'continuous') {
      instStr = `cont ${inst.collection || '?'}`;
    } else {
      // spot or anything else we can't recognise — best effort.
      const col = inst.collection || '?';
      const sym = inst.instrument_id || '?';
      instStr = `${col}:${sym}`;
    }
  }
  return inputId ? `${inputId} • ${instStr}` : instStr;
}

function SignalChart({ result, loading, error }) {
  const { traces, layoutOverrides, hasData, clipSummary } = useMemo(() => {
    if (!result || !Array.isArray(result.timestamps) || result.timestamps.length === 0) {
      return { traces: [], layoutOverrides: {}, hasData: false, clipSummary: null };
    }
    const positions = Array.isArray(result.positions) ? result.positions : [];
    if (positions.length === 0) {
      return { traces: [], layoutOverrides: {}, hasData: false, clipSummary: null };
    }
    // Convert unix-ms timestamps to Date objects so Plotly treats them
    // as a datetime axis.
    const dates = result.timestamps.map((ms) => new Date(ms));

    const N = positions.length;
    const traces = [];
    const lo = {
      showlegend: true,
      legend: { orientation: 'h', y: -0.08 },
    };

    // Divide vertical space into N rows; reserve small gaps between them.
    const gap = 0.04;
    const bandHeight = (1 - gap * (N - 1)) / N;

    const clipRows = [];

    positions.forEach((p, idx) => {
      // Plotly stacks subplots top-to-bottom visually when idx 0 has
      // the HIGHEST y-domain. Row 0 → top; row N-1 → bottom.
      const topEdge = 1 - idx * (bandHeight + gap);
      const bottomEdge = topEdge - bandHeight;
      const yKey = idx === 0 ? 'y' : `y${idx + 1}`;
      const yAxisName = idx === 0 ? 'yaxis' : `yaxis${idx + 1}`;
      const xAnchor = yKey;
      const instLabel = formatInstrumentLabel(p);
      const posTrace = {
        x: dates,
        y: p.values,
        type: 'scatter',
        mode: 'lines',
        name: `pos — ${instLabel}`,
        yaxis: yKey,
        line: { color: TRACE_COLORS[idx] || '#f59e0b', width: 1.5 },
        fill: 'tozeroy',
        fillcolor: `rgba(245, 158, 11, 0.12)`,
        hovertemplate: '%{x}<br>%{y:.3f}<extra></extra>',
        connectgaps: false,
      };
      traces.push(posTrace);

      lo[yAxisName] = {
        title: { text: instLabel, font: { size: 10 } },
        domain: [Math.max(0, bottomEdge), Math.min(1, topEdge)],
        range: [-1.1, 1.1],
        zeroline: true,
      };

      // If price present, overlay on a right axis with the same domain.
      if (p.price && Array.isArray(p.price.values)) {
        const priceYKey = idx === 0 ? 'y1r' : `y${idx + 1}r`; // logical label
        const priceAxisName = `yaxis${N + idx + 1}`;
        const priceAxisId = `y${N + idx + 1}`;
        traces.push({
          x: dates,
          y: p.price.values,
          type: 'scatter',
          mode: 'lines',
          name: `price — ${p.price.label}`,
          yaxis: priceAxisId,
          line: { color: TRACE_COLORS[(idx + 3) % TRACE_COLORS.length] || '#2563eb', width: 1 },
          hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
          connectgaps: false,
          opacity: 0.7,
        });
        lo[priceAxisName] = {
          domain: [Math.max(0, bottomEdge), Math.min(1, topEdge)],
          overlaying: yKey,
          side: 'right',
          showgrid: false,
        };
      }

      // The bottom-most subplot owns the x-axis anchor; everything else
      // hides its own tick labels so the axis appears at the bottom only.
      if (idx === N - 1) {
        lo.xaxis = { anchor: xAnchor };
      } else {
        lo[yAxisName].showticklabels = true;
      }

      // Clip counting
      const mask = Array.isArray(p.clipped_mask) ? p.clipped_mask : [];
      const clipCount = mask.reduce((n, b) => (b ? n + 1 : n), 0);
      if (clipCount > 0) {
        clipRows.push({ instrument: instLabel, count: clipCount });
      }
    });

    const clipSummary = result.clipped ? { rows: clipRows } : null;

    return {
      traces,
      layoutOverrides: lo,
      hasData: true,
      clipSummary,
    };
  }, [result]);

  if (loading) {
    return (
      <div className={styles.chartPanelBody}>
        <div className={styles.chartState}>Computing…</div>
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
        <div className={styles.chartState} data-testid="signal-chart-empty">
          Run a signal to see positions
        </div>
      </div>
    );
  }

  return (
    <div className={styles.chartPanelBody}>
      {clipSummary && (
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
      <div className={styles.chartWrap} data-testid="signal-chart-multi">
        <Chart
          traces={traces}
          layoutOverrides={layoutOverrides}
          className={styles.chart}
          downloadFilename="signal-positions"
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
