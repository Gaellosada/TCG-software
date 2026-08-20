import { useMemo } from 'react';
import Card from '../../components/Card';
import Chart from '../../components/Chart';
import useTheme from '../../hooks/useTheme';
import { getChartColors } from '../../utils/chartTheme';
import { formatCurrency, formatPercent } from '../../utils/format';
import { computeLadderAnalysis } from './ladderAnalysis';
import styles from './IntradayBacktestPage.module.css';

// A4 — Ladder entry-time / rung PnL attribution (W5/P4, unlocked by F4.1
// laddered multi-entry).
//
// PURE IN-APP VIEW over the existing run response's `days[]` (F4.1's
// per-rung `entries[]`, see ladderAnalysis.js) — no engine, schema, or
// serializer change. Answers "which rung / entry time performs best?" by
// aggregating every day's rungs by RUNG INDEX (DST-safe — see
// ladderAnalysis.js for why raw UTC time-of-day is NOT used as the bucket
// key) and showing mean P&L per rung.
//
// Graceful degradation: a non-laddered run (no per-entry rows) or a run
// where every laddered day has only one rung renders a concise hint instead
// of a broken/empty chart — this view is inert next to LadderEntriesView,
// which stays the per-run/per-rung readout; this one is the cross-day
// attribution.
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
  no_entries: 'This run has no per-entry (per-rung) data. Enable laddered entry '
    + '(30-min-ladder hold-to-settlement) above and re-run the backtest to see '
    + 'PnL attribution by entry time-of-day.',
  single_rung: 'Every laddered day in this run has only a single rung, so there is '
    + 'nothing to compare rung-to-rung. Widen the ladder window (earlier first '
    + 'entry / later cutoff, or a shorter interval) to see per-rung attribution.',
};

export default function LadderAnalysisView({ days }) {
  const result = useMemo(() => computeLadderAnalysis(days), [days]);
  const theme = useTheme();
  const { success, error, secondaryFont } = useMemo(() => getChartColors(theme), [theme]);

  if (!result.available) {
    return (
      <Card
        title="Ladder analysis (PnL by entry time)"
        className={styles.resultCard}
        bodyClassName={styles.cardBody}
        data-result-card="true"
        data-testid="ladder-analysis-view"
      >
        <p className={styles.help} data-testid="ladder-analysis-hint">
          {HINT_TEXT[result.reason] || HINT_TEXT.no_entries}
        </p>
      </Card>
    );
  }

  const { buckets, overall } = result;
  const barTraces = [{
    x: buckets.map((b) => `${b.label}${b.timeVaries ? '*' : ''}`),
    y: buckets.map((b) => b.meanUsd),
    type: 'bar',
    name: 'Mean P&L / rung (USD)',
    marker: {
      color: buckets.map((b) => {
        if (typeof b.meanUsd !== 'number' || !Number.isFinite(b.meanUsd)) return secondaryFont;
        return b.meanUsd >= 0 ? success : error;
      }),
    },
    text: buckets.map((b) => `N=${b.n}`),
    textposition: 'outside',
    customdata: buckets.map((b) => b.n),
    hovertemplate: '<b>%{x}</b><br>Mean P&L: %{y:$,.0f}<br>N=%{customdata}<extra></extra>',
  }];

  return (
    <Card
      title="Ladder analysis (PnL by entry time)"
      className={styles.resultCard}
      bodyClassName={styles.cardBody}
      data-result-card="true"
      data-testid="ladder-analysis-view"
    >
      <p className={styles.help} data-testid="ladder-analysis-caveat">
        Post-hoc breakdown of the run above: per-rung P&amp;L (F4.1&apos;s laddered
        entries) grouped by RUNG POSITION (1st entry of the day, 2nd, …) across all
        laddered days — not by raw clock time, since a rung&apos;s UTC entry time
        shifts by an hour across DST. A label marked <strong>*</strong> means that
        rung&apos;s UTC entry time actually varied across days (e.g. a DST change).
        The data window is only ~1-1.5 years, so each rung rests on a small
        sample — the N label and table column show exactly how many traded
        entries back each figure; read thin buckets (low N) as suggestive, not
        robust.
        {overall && (
          <>
            {' '}Overall: {overall.nDays} laddered day{overall.nDays === 1 ? '' : 's'},
            {' '}{overall.nTraded} traded entries, total {fmtUsd(overall.sumUsd)}.
          </>
        )}
      </p>

      <div className={styles.chartBox}>
        <Chart
          traces={barTraces}
          style={{ width: '100%', height: '100%' }}
          downloadFilename="intraday-backtest-ladder-analysis"
          layoutOverrides={{
            xaxis: { type: 'category', title: 'Entry time (rung)' },
            yaxis: { title: 'Mean P&L per rung (USD)' },
            showlegend: false,
          }}
        />
      </div>

      <div className={styles.calendar} style={{ marginTop: '1rem', overflowX: 'auto' }}>
        <table data-testid="ladder-analysis-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Rung</th>
              <th style={{ textAlign: 'left' }}>Entry time</th>
              <th style={{ textAlign: 'right' }}>N</th>
              <th style={{ textAlign: 'right' }}>Skipped</th>
              <th style={{ textAlign: 'right' }}>Total P&L</th>
              <th style={{ textAlign: 'right' }}>Mean</th>
              <th style={{ textAlign: 'right' }}>Win rate</th>
            </tr>
          </thead>
          <tbody>
            {buckets.map((b) => (
              <tr key={b.rung} data-testid={`ladder-analysis-row-${b.rung}`} data-n={b.n}>
                <td>{b.rung + 1}</td>
                <td>{b.label}{b.timeVaries ? '*' : ''}</td>
                <td style={{ textAlign: 'right' }}>{b.n}</td>
                <td style={{ textAlign: 'right' }}>{b.nSkipped}</td>
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
