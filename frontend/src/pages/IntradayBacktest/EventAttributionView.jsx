import { useMemo } from 'react';
import Card from '../../components/Card';
import Chart from '../../components/Chart';
import useTheme from '../../hooks/useTheme';
import { getChartColors } from '../../utils/chartTheme';
import { formatCurrency, formatPercent } from '../../utils/format';
import { computeEventAttribution } from './eventAttribution';
import styles from './IntradayBacktestPage.module.css';

// A3 — Event-day attribution view (W4/P3, unlocked by F3.1 curated event
// calendar).
//
// PURE IN-APP VIEW over the existing run response's `days[]` joined against
// the F3.1 event-calendar endpoint payload (already fetched once by the page
// for the allowlist control — see eventAttribution.js) — no engine, schema,
// or serializer change. Answers "is performance concentrated on FOMC/NFP/CPI
// days, and which structurally hurt?" via per-event-type buckets plus the
// key any-event-vs-non-event comparison.
//
// Multi-membership: a day matching more than one event type (a rare
// FOMC+CPI overlap) is counted in EACH matching type bar/row, but only ONCE
// in the event-vs-non-event comparison (see eventAttribution.js docstring).
//
// Graceful degradation: if the event-calendar endpoint failed/hasn't loaded
// (`eventCalendar` is null) or loaded but no curated event date falls within
// this run's traded days, `computeEventAttribution` returns
// `available: false` and this view renders a concise hint instead of an
// empty/broken chart.
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

const HINT_TEXT = {
  no_calendar: 'The curated event calendar (FOMC/NFP/CPI) is unavailable, so per-day '
    + 'PnL cannot be joined against event dates for this run.',
  no_overlap: 'None of the curated FOMC/NFP/CPI dates fall within this run’s traded '
    + 'days, so there is no event-day attribution to show.',
};

export default function EventAttributionView({ days, eventCalendar }) {
  const result = useMemo(() => computeEventAttribution(days, eventCalendar), [days, eventCalendar]);
  const theme = useTheme();
  const { success, error } = useMemo(() => getChartColors(theme), [theme]);

  if (!result.available) {
    return (
      <Card
        title="Event-day attribution"
        className={styles.resultCard}
        bodyClassName={styles.cardBody}
        data-result-card="true"
        data-testid="event-attribution-view"
      >
        <p className={styles.help} data-testid="event-attribution-hint">
          {HINT_TEXT[result.reason] || HINT_TEXT.no_calendar}
        </p>
      </Card>
    );
  }

  const rows = [...result.typeBuckets, ...result.comparison];
  const barTraces = [{
    x: rows.map((b) => b.label),
    y: rows.map((b) => b.sumUsd),
    type: 'bar',
    name: 'Total P&L (USD)',
    marker: { color: rows.map((b) => (b.sumUsd >= 0 ? success : error)) },
    text: rows.map((b) => `N=${b.n}`),
    textposition: 'outside',
    customdata: rows.map((b) => b.n),
    hovertemplate: '<b>%{x}</b><br>Total P&L: %{y:$,.0f}<br>N=%{customdata}<extra></extra>',
  }];

  return (
    <Card
      title="Event-day attribution"
      className={styles.resultCard}
      bodyClassName={styles.cardBody}
      data-result-card="true"
      data-testid="event-attribution-view"
    >
      <p className={styles.help} data-testid="event-attribution-caveat">
        Post-hoc breakdown of the run above: per-day P&amp;L joined against the curated
        FOMC/NFP/CPI event calendar. A day matching more than one event type (a rare
        overlap) is counted in EACH matching type bar, but only once in the &quot;Event
        day&quot; vs &quot;Non-event day&quot; comparison. The data window is only ~1-1.5
        years, so each bucket rests on a small sample — the N label and table column show
        exactly how many traded days back each figure; read thin buckets (low N) as
        suggestive, not robust.
      </p>

      <div className={styles.chartBox}>
        <Chart
          traces={barTraces}
          style={{ width: '100%', height: '100%' }}
          downloadFilename="intraday-backtest-event-attribution"
          layoutOverrides={{
            xaxis: { type: 'category', title: 'Bucket' },
            yaxis: { title: 'Total P&L (USD)' },
            showlegend: false,
          }}
        />
      </div>

      <div className={styles.calendar} style={{ marginTop: '1rem', overflowX: 'auto' }}>
        <table data-testid="event-attribution-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Bucket</th>
              <th style={{ textAlign: 'right' }}>N</th>
              <th style={{ textAlign: 'right' }}>Total P&L</th>
              <th style={{ textAlign: 'right' }}>Mean</th>
              <th style={{ textAlign: 'right' }}>Win rate</th>
            </tr>
          </thead>
          <tbody>
            {result.typeBuckets.map((b) => (
              <tr key={b.key} data-testid={`event-attribution-row-${b.key}`} data-n={b.n}>
                <td>{b.label}</td>
                <td style={{ textAlign: 'right' }}>{b.n}</td>
                <td style={{ textAlign: 'right' }} className={signClass(b.sumUsd)}>{fmtUsd(b.sumUsd)}</td>
                <td style={{ textAlign: 'right' }} className={signClass(b.meanUsd)}>{fmtUsd(b.meanUsd)}</td>
                <td style={{ textAlign: 'right' }}>{fmtPct(b.winRate)}</td>
              </tr>
            ))}
            {result.comparison.map((b) => (
              <tr key={b.key} data-testid={`event-attribution-row-${b.key}`} data-n={b.n}>
                <td><strong>{b.label}</strong></td>
                <td style={{ textAlign: 'right' }}>{b.n}</td>
                <td style={{ textAlign: 'right' }} className={signClass(b.sumUsd)}>{fmtUsd(b.sumUsd)}</td>
                <td style={{ textAlign: 'right' }} className={signClass(b.meanUsd)}>{fmtUsd(b.meanUsd)}</td>
                <td style={{ textAlign: 'right' }}>{fmtPct(b.winRate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
