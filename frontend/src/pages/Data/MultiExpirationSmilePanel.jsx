import { useState, useEffect, useRef } from 'react';
import Chart from '../../components/Chart';
import { getChainSnapshot } from '../../api/options';
import styles from './MultiExpirationSmilePanel.module.css';

// ---------------------------------------------------------------------------
// 8-color palette — distinct, high-contrast, perceptually varied.
// Chosen to remain distinguishable in both dark and light themes.
// ---------------------------------------------------------------------------
export const SMILE_PALETTE = [
  '#0ea5e9', // sky
  '#f59e0b', // amber
  '#10b981', // emerald
  '#ef4444', // red
  '#8b5cf6', // violet
  '#ec4899', // pink
  '#06b6d4', // cyan
  '#f97316', // orange
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Given the API response, build one Plotly scatter trace per expiration.
 * The API returns `{ series: [ { expiration, strikes, values } ] }`.
 * A null value in `values` represents a missing point — Plotly will gap-connect.
 */
function buildTraces(series, field) {
  if (!Array.isArray(series) || series.length === 0) return [];

  return series.map((s, i) => {
    const color = SMILE_PALETTE[i % SMILE_PALETTE.length];
    const ys = Array.isArray(s.values)
      ? s.values.map((v) => {
          if (v == null) return null;
          // ComputeResult wrapper: { value, source, ... }
          if (typeof v === 'object' && 'value' in v) {
            return v.source === 'missing' || v.value == null ? null : Number(v.value);
          }
          return Number(v);
        })
      : [];

    return {
      x: Array.isArray(s.strikes) ? s.strikes : [],
      y: ys,
      type: 'scatter',
      mode: 'lines+markers',
      name: s.expiration || `Exp ${i + 1}`,
      line: { color, width: 1.5 },
      marker: { color, size: 4 },
      connectgaps: false,
      hovertemplate: `Strike: %{x}<br>${field === 'iv' ? 'IV' : 'Δ'}: %{y:.4f}<extra>${s.expiration || ''}</extra>`,
    };
  });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * Renders up to 8 IV-vs-strike (or Δ-vs-strike) traces, one per expiration,
 * all on the same chart.
 *
 * Props:
 *   root        {string}   option root (e.g. 'OPT_SP_500')
 *   date        {string}   snapshot date YYYY-MM-DD
 *   type        {'C'|'P'}  option type (default 'C')
 *   expirations {string[]} array of YYYY-MM-DD; max 8 (backend-enforced)
 *   onClose     {Function} called when the close button is clicked
 */
export default function MultiExpirationSmilePanel({
  root,
  date,
  type = 'C',
  expirations,
  onClose,
}) {
  const [field, setField] = useState('iv');
  const [series, setSeries] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Track the latest request to avoid stale state on fast toggling.
  const fetchIdRef = useRef(0);

  useEffect(() => {
    if (!root || !date || !Array.isArray(expirations) || expirations.length === 0) {
      setSeries(null);
      return;
    }

    const id = ++fetchIdRef.current;
    setLoading(true);
    setError(null);

    getChainSnapshot(root, { date, type, expirations, field })
      .then((resp) => {
        if (fetchIdRef.current !== id) return;
        setSeries(resp && Array.isArray(resp.series) ? resp.series : []);
        setLoading(false);
      })
      .catch((err) => {
        if (fetchIdRef.current !== id) return;
        setError(err);
        setLoading(false);
      });
  }, [root, date, type, expirations, field]);

  const isEmpty = !Array.isArray(expirations) || expirations.length === 0;

  const traces = series != null ? buildTraces(series, field) : [];

  const layoutOverrides = {
    xaxis: {
      title: { text: 'Strike', font: { size: 11 } },
      type: 'linear',
    },
    yaxis: {
      title: {
        text: field === 'iv' ? 'Implied Volatility' : 'Delta',
        font: { size: 11 },
      },
    },
    legend: { orientation: 'h', yanchor: 'top', y: -0.15, xanchor: 'center', x: 0.5 },
  };

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>
          Multi-Expiration Smile — {root}
        </h2>
        <button type="button" className={styles.closeButton} onClick={onClose}>
          Close
        </button>
      </div>

      <div className={styles.controls}>
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
            Δ
          </button>
        </div>
      </div>

      {loading && <div className={styles.loading}>Loading smile data…</div>}
      {error && !loading && (
        <div className={styles.error}>
          Failed to load smile: {error.message || String(error)}
        </div>
      )}

      {!loading && !error && (
        <div className={styles.chartCard}>
          {isEmpty ? (
            <div className={styles.hint}>
              Select up to 8 expirations to display the smile.
            </div>
          ) : traces.length === 0 && series != null ? (
            <div className={styles.empty}>No data available for the selected expirations.</div>
          ) : (
            <Chart
              traces={traces}
              layoutOverrides={layoutOverrides}
              className={styles.chartWrapper}
              downloadFilename={`${root}-smile-${date}`}
            />
          )}
        </div>
      )}
    </div>
  );
}
