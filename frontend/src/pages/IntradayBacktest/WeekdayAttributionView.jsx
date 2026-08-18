import { useMemo } from 'react';
import Card from '../../components/Card';
import Chart from '../../components/Chart';
import { formatCurrency, formatNumber, formatPercent } from '../../utils/format';
import { groupPnlByWeekday } from './weekdayAttribution';
import styles from './IntradayBacktestPage.module.css';

// A1 — Weekday-attribution view (W2/P1, HANDOFF.md §3 / §4 Phase 1).
//
// PURE IN-APP VIEW over the existing run response's `days[]` — no engine,
// schema, or serializer change (see weekdayAttribution.js). Groups per-day
// PnL by weekday and renders it as a bar chart (via the shared Chart
// component, so theming/CSV-export come for free) plus a breakdown table.
//
// Robustness: the intraday option data window is only ~1-1.5yr, so each
// weekday bucket has at most ~60-75 trading days and often fewer once
// skips/exclusions are removed. The per-weekday N is rendered on the chart
// bars (as text) AND in the table so a reader never mistakes a thin bucket
// for a robust result — this is a caveat, not a general framework.
function signClass(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '';
  if (v > 0) return styles.positive;
  if (v < 0) return styles.negative;
  return '';
}

function fmtUsd(v) {
  return typeof v === 'number' && Number.isFinite(v) ? formatCurrency(v) : '—';
}

function fmtPts(v) {
  return typeof v === 'number' && Number.isFinite(v) ? `${formatNumber(v)} pts` : '—';
}

function fmtPct(v) {
  return typeof v === 'number' && Number.isFinite(v) ? formatPercent(v) : '—';
}

export default function WeekdayAttributionView({ days }) {
  const buckets = useMemo(() => groupPnlByWeekday(days), [days]);

  const barTraces = useMemo(() => [{
    x: buckets.map((b) => b.weekday),
    y: buckets.map((b) => b.sumUsd),
    type: 'bar',
    name: 'Total P&L (USD)',
    marker: { color: buckets.map((b) => (b.sumUsd >= 0 ? '#10b981' : '#ef4444')) },
    text: buckets.map((b) => `N=${b.n}`),
    textposition: 'outside',
    customdata: buckets.map((b) => b.n),
    hovertemplate: '<b>%{x}</b><br>Total P&L: %{y:$,.0f}<br>N=%{customdata}<extra></extra>',
  }], [buckets]);

  return (
    <Card
      title="Weekday attribution"
      className={styles.resultCard}
      bodyClassName={styles.cardBody}
      data-result-card="true"
      data-testid="weekday-attribution-view"
    >
      <p className={styles.help} data-testid="weekday-attribution-caveat">
        Post-hoc breakdown of the run above, grouped by weekday. The data window is
        only ~1-1.5 years, so each bar rests on a small sample — the N column and
        bar labels below show exactly how many traded days back each figure;
        read thin buckets (low N) as suggestive, not robust.
      </p>

      <div className={styles.chartBox}>
        <Chart
          traces={barTraces}
          style={{ width: '100%', height: '100%' }}
          downloadFilename="intraday-backtest-weekday-attribution"
          layoutOverrides={{
            xaxis: { type: 'category', title: 'Weekday' },
            yaxis: { title: 'Total P&L (USD)' },
            showlegend: false,
          }}
        />
      </div>

      <div className={styles.calendar} style={{ marginTop: '1rem', overflowX: 'auto' }}>
        <table data-testid="weekday-attribution-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Weekday</th>
              <th style={{ textAlign: 'right' }}>N</th>
              <th style={{ textAlign: 'right' }}>Total P&L</th>
              <th style={{ textAlign: 'right' }}>Total P&L (pts)</th>
              <th style={{ textAlign: 'right' }}>Mean</th>
              <th style={{ textAlign: 'right' }}>Median</th>
              <th style={{ textAlign: 'right' }}>Win rate</th>
            </tr>
          </thead>
          <tbody>
            {buckets.map((b) => (
              <tr key={b.weekday} data-testid={`weekday-row-${b.weekday}`} data-n={b.n}>
                <td>{b.weekday}</td>
                <td style={{ textAlign: 'right' }}>{b.n}</td>
                <td style={{ textAlign: 'right' }} className={signClass(b.sumUsd)}>{fmtUsd(b.sumUsd)}</td>
                <td style={{ textAlign: 'right' }}>{fmtPts(b.sumPts)}</td>
                <td style={{ textAlign: 'right' }} className={signClass(b.meanUsd)}>{fmtUsd(b.meanUsd)}</td>
                <td style={{ textAlign: 'right' }} className={signClass(b.medianUsd)}>{fmtUsd(b.medianUsd)}</td>
                <td style={{ textAlign: 'right' }}>{fmtPct(b.winRate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
