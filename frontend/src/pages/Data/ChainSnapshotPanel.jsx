import { useState, useMemo } from 'react';
import useAsync from '../../hooks/useAsync';
import Chart from '../../components/Chart';
import { getChainSnapshot } from '../../api/options';
import { createVerticalLineTrace, hiddenOverlayAxis } from '../../utils/chartTheme';
import styles from './ChainSnapshotPanel.module.css';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Extract (x, y) series from a SmileSeries' points for the given xAxis mode.
 * Points where value.value === null (source='missing') are skipped by returning
 * null for y — Plotly renders a gap when connectgaps: false.
 */
function buildXY(points, xAxis) {
  const xs = [];
  const ys = [];
  for (const pt of points) {
    const x = xAxis === 'K_over_S' ? pt.K_over_S : pt.strike;
    const y = pt.value != null && pt.value.value !== null ? Number(pt.value.value) : null;
    xs.push(x);
    ys.push(y);
  }
  return { xs, ys };
}

function chartTitle(root, date, expiration, field) {
  const fieldLabel = field === 'delta' ? 'Delta' : 'IV';
  return `${root} — ${date} — exp ${expiration} — ${fieldLabel}`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * ChainSnapshotPanel — single-date IV-vs-strike (or delta-vs-strike) trace
 * for one expiration.
 *
 * Props:
 *   root       string  — option root (e.g. 'OPT_SP_500')
 *   date       string  — YYYY-MM-DD
 *   type       'C'|'P' — option type (default 'C')
 *   expiration string  — single expiration date YYYY-MM-DD
 *   onClose    fn      — called when user clicks Close
 */
export default function ChainSnapshotPanel({
  root,
  date,
  type = 'C',
  expiration,
  onClose,
}) {
  const [field, setField] = useState('iv');
  const [xAxis, setXAxis] = useState('strike');

  // Re-fetch whenever root / date / type / expiration / field changes.
  // xAxis toggle is client-side only — no re-fetch needed.
  const { data, loading, error } = useAsync(
    () => getChainSnapshot(root, { date, type, expirations: [expiration], field }),
    [root, date, type, expiration, field],
  );

  const { traces, hasAtmLine } = useMemo(() => {
    if (!data || !Array.isArray(data.series) || data.series.length === 0) {
      return { traces: [], hasAtmLine: false };
    }
    // Single expiration: take the first (and only) series entry.
    const series = data.series[0];
    if (!series || !Array.isArray(series.points)) {
      return { traces: [], hasAtmLine: false };
    }

    const { xs, ys } = buildXY(series.points, xAxis);

    const xLabel = xAxis === 'K_over_S' ? 'K/S' : 'Strike';
    const yLabel = field === 'delta' ? 'Delta' : 'IV';

    const traceList = [
      {
        x: xs,
        y: ys,
        type: 'scatter',
        mode: 'lines+markers',
        name: yLabel,
        connectgaps: false,
        // Line drawn at 0.5 alpha so the markers (actual data points)
        // dominate visually and the smile curve reads as a guide rather
        // than the primary content.
        line: { width: 1.5, color: 'rgba(14, 165, 233, 0.5)' },
        marker: { size: 5, color: '#0ea5e9' },
        hovertemplate: `${xLabel}: %{x}<br>${yLabel}: %{y:.4f}<extra></extra>`,
      },
    ];

    // ATM marker — vertical line where the option is exactly ATM.
    //   K/S axis: K = S → x = 1.
    //   Strike axis: x = S (the underlying price for that day).
    const underlyingValue =
      data.underlying_price && data.underlying_price.value != null
        ? Number(data.underlying_price.value)
        : null;
    let atmX = null;
    if (xAxis === 'K_over_S') {
      atmX = 1;
    } else if (underlyingValue != null) {
      atmX = underlyingValue;
    }
    let atmAdded = false;
    if (atmX != null) {
      const atmLabel =
        xAxis === 'K_over_S'
          ? 'ATM (K = S)'
          : `ATM (S = ${atmX.toFixed(2)})`;
      traceList.push(
        createVerticalLineTrace([atmX], {
          name: atmLabel,
          color: '#f59e0b',
          dash: 'dash',
          yaxisKey: 'y2',
        }),
      );
      atmAdded = true;
    }

    return { traces: traceList, hasAtmLine: atmAdded };
  }, [data, xAxis, field]);

  const layoutOverrides = useMemo(() => ({
    title: {
      text: root && date && expiration
        ? chartTitle(root, date, expiration, field)
        : '',
      font: { size: 13 },
    },
    xaxis: {
      title: { text: xAxis === 'K_over_S' ? 'K / S' : 'Strike', font: { size: 11 } },
      // Override the global default (type='date' in chartTheme.buildBaseLayout)
      // — this chart's x-axis is numeric (strike or K/S), and on K/S the
      // small fractional values look nonsensical as dates.
      type: 'linear',
    },
    yaxis: { title: { text: field === 'delta' ? 'Delta' : 'IV', font: { size: 11 } } },
    ...(hasAtmLine ? { yaxis2: hiddenOverlayAxis() } : {}),
  }), [root, date, expiration, field, xAxis, hasAtmLine]);

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>
          Smile Snapshot
        </h2>
      </div>

      <div className={styles.controls}>
        {/* Field toggle */}
        <span className={styles.controlLabel}>Field</span>
        <div className={styles.toggleGroup}>
          <button
            type="button"
            className={`${styles.toggleButton} ${field === 'iv' ? styles.toggleButtonActive : ''}`}
            onClick={() => setField('iv')}
          >
            IV
          </button>
          <button
            type="button"
            className={`${styles.toggleButton} ${field === 'delta' ? styles.toggleButtonActive : ''}`}
            onClick={() => setField('delta')}
          >
            Delta
          </button>
        </div>

        {/* xAxis toggle */}
        <span className={styles.controlLabel}>X axis</span>
        <div className={styles.toggleGroup}>
          <button
            type="button"
            className={`${styles.toggleButton} ${xAxis === 'strike' ? styles.toggleButtonActive : ''}`}
            onClick={() => setXAxis('strike')}
          >
            Strike
          </button>
          <button
            type="button"
            className={`${styles.toggleButton} ${xAxis === 'K_over_S' ? styles.toggleButtonActive : ''}`}
            onClick={() => setXAxis('K_over_S')}
          >
            K/S
          </button>
        </div>
      </div>

      {loading && (
        <div className={styles.loading}>Loading snapshot…</div>
      )}
      {error && (
        <div className={styles.error}>
          Failed to load snapshot: {error.message || String(error)}
        </div>
      )}

      {!loading && !error && (
        <div className={styles.chartCard}>
          {traces.length > 0 ? (
            <Chart
              traces={traces}
              layoutOverrides={layoutOverrides}
              className={styles.chartWrapper}
              downloadFilename={`${root}-${date}-${expiration}-${field}`}
            />
          ) : (
            <div className={styles.empty}>
              {data ? 'No data for this expiration.' : ''}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
