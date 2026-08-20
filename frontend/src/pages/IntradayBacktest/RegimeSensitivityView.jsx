import { useMemo, useState } from 'react';
import Card from '../../components/Card';
import Chart from '../../components/Chart';
import useTheme from '../../hooks/useTheme';
import { getChartColors } from '../../utils/chartTheme';
import { formatCurrency, formatPercent } from '../../utils/format';
import { computeRegimeSensitivity } from './regimeSensitivity';
import styles from './IntradayBacktestPage.module.css';

// A2 — Regime-sensitivity view (W3/P2, unlocked by F2.1 RV/VVIX signals +
// F2.2 regime-driven side).
//
// PURE IN-APP VIEW over the existing run response's `days[]` — no engine,
// schema, or serializer change (see regimeSensitivity.js). Shows how per-day
// PnL relates to the vol-regime state (bucketed) and to two continuous
// regime signals (scatter): VVIX and an RV term-structure spread (H20-H100).
//
// Robustness: same ~1-1.5yr data window as the weekday view, so state
// buckets can be thin (especially extremely_low/fallback) — the N column
// makes that explicit rather than dressing a small sample as robust.
const SIGNAL_OPTIONS = [
  { key: 'vvix', label: 'VVIX', xTitle: 'VVIX (level)' },
  { key: 'rvSpread', label: 'RV term structure (H20 − H100)', xTitle: 'H20 − H100 (annualized RV)' },
];

function signClass(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '';
  if (v > 0) return styles.positive;
  if (v < 0) return styles.negative;
  return '';
}

function fmtUsd(v) {
  return typeof v === 'number' && Number.isFinite(v) ? formatCurrency(v) : '—';
}

function fmtPct(v) {
  return typeof v === 'number' && Number.isFinite(v) ? formatPercent(v) : '—';
}

export default function RegimeSensitivityView({ days }) {
  const result = useMemo(() => computeRegimeSensitivity(days), [days]);
  const theme = useTheme();
  const { success } = useMemo(() => getChartColors(theme), [theme]);
  const [signalKey, setSignalKey] = useState('vvix');

  if (!result.available) {
    return (
      <Card
        title="Regime sensitivity"
        className={styles.resultCard}
        bodyClassName={styles.cardBody}
        data-result-card="true"
        data-testid="regime-sensitivity-view"
      >
        <p className={styles.help} data-testid="regime-sensitivity-hint">
          No vol-regime data on this run. Enable regime signals (RV H20/H30/H100 + VVIX)
          or a regime-driven side above, then re-run the backtest, to see how per-day
          P&amp;L relates to the vol regime.
        </p>
      </Card>
    );
  }

  const option = SIGNAL_OPTIONS.find((o) => o.key === signalKey) || SIGNAL_OPTIONS[0];
  const points = result.scatter[signalKey] || [];

  const scatterTraces = [{
    x: points.map((p) => p.x),
    y: points.map((p) => p.y),
    type: 'scatter',
    mode: 'markers',
    name: 'Day P&L',
    marker: { color: success, size: 7, opacity: 0.75 },
    text: points.map((p) => p.date),
    hovertemplate: `<b>%{text}</b><br>${option.label}: %{x}<br>P&L: %{y:$,.0f}<extra></extra>`,
  }];

  return (
    <Card
      title="Regime sensitivity"
      className={styles.resultCard}
      bodyClassName={styles.cardBody}
      data-result-card="true"
      data-testid="regime-sensitivity-view"
    >
      <p className={styles.help} data-testid="regime-sensitivity-caveat">
        Post-hoc breakdown of the run above: per-day P&amp;L joined against its vol-regime
        state and signals. The data window is only ~1-1.5 years, so each state bucket rests
        on a small sample — the N column shows exactly how many traded days back each
        figure; read thin buckets (low N) as suggestive, not robust.
      </p>

      <div className={styles.calendar} style={{ overflowX: 'auto' }}>
        <table data-testid="regime-sensitivity-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Regime state</th>
              <th style={{ textAlign: 'right' }}>N</th>
              <th style={{ textAlign: 'right' }}>Total P&L</th>
              <th style={{ textAlign: 'right' }}>Mean</th>
              <th style={{ textAlign: 'right' }}>Win rate</th>
            </tr>
          </thead>
          <tbody>
            {result.buckets.map((b) => (
              <tr key={b.state} data-testid={`regime-row-${b.state}`} data-n={b.n}>
                <td>{b.label}</td>
                <td style={{ textAlign: 'right' }}>{b.n}</td>
                <td style={{ textAlign: 'right' }} className={signClass(b.sumUsd)}>{fmtUsd(b.sumUsd)}</td>
                <td style={{ textAlign: 'right' }} className={signClass(b.meanUsd)}>{fmtUsd(b.meanUsd)}</td>
                <td style={{ textAlign: 'right' }}>{fmtPct(b.winRate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <label htmlFor="regime-signal-select" className={styles.help}>P&amp;L vs</label>
        <select
          id="regime-signal-select"
          data-testid="regime-signal-select"
          value={signalKey}
          onChange={(e) => setSignalKey(e.target.value)}
        >
          {SIGNAL_OPTIONS.map((o) => (
            <option key={o.key} value={o.key}>{o.label}</option>
          ))}
        </select>
      </div>

      <div className={styles.chartBox}>
        <Chart
          traces={scatterTraces}
          style={{ width: '100%', height: '100%' }}
          downloadFilename={`intraday-backtest-regime-${signalKey}`}
          layoutOverrides={{
            xaxis: { title: option.xTitle, type: 'linear' },
            yaxis: { title: 'Day P&L (USD)' },
            showlegend: false,
          }}
        />
      </div>
    </Card>
  );
}
