import { useEffect, useMemo, useRef, useState } from 'react';
import { fetchStatistics } from '../../api/statistics';
import RiskFreeRateInput from '../RiskFreeRateInput';
import styles from './Statistics.module.css';

// Debounce window (ms) before a new Rf input value triggers a refetch.
const RF_DEBOUNCE_MS = 300;

const DEFAULT_RF = 0.04;

// Section/metric layout. Each entry: {key, label, format, [tooltip]}.
//
// ``format`` is one of:
//   - 'ratio'      → 2 decimals, no unit, no sign coloring
//   - 'percent'    → ``XX.YY%`` with sign coloring (input is a FRACTION, ×100)
//   - 'pct_value'  → ``XX.YY%`` neutral, input is ALREADY a percent (no scaling)
//   - 'days'       → integer + " days"
// Null values always render as "—".
//
// The 'pct_value' branch exists for the Costs rows: the backend returns
// ``total_slippage_paid_pct`` / ``total_fees_paid_pct`` already in percent
// units, so they must NOT be multiplied by 100 the way the fraction-based
// 'percent' metrics are. Costs are magnitudes (always ≥ 0) so they render
// unsigned and uncoloured — a cost is neither a gain nor a loss.
const SECTIONS = [
  {
    key: 'return',
    title: 'Return',
    metrics: [
      { key: 'total_return',          label: 'Total Return',  format: 'percent', tooltip: 'Cumulative return over the period.' },
      { key: 'excess_return',         label: 'Excess Return', format: 'percent', tooltip: 'CAGR minus the risk-free rate.' },
      { key: 'cagr',                  label: 'CAGR',          format: 'percent', tooltip: 'Compound Annual Growth Rate.' },
      { key: 'annualized_volatility', label: 'Ann. Vol',      format: 'percent', tooltip: 'Annualized standard deviation of daily returns.' },
      { key: 'best_day',              label: 'Best Day',      format: 'percent', tooltip: 'Largest single-day return.' },
      { key: 'worst_day',             label: 'Worst Day',     format: 'percent', tooltip: 'Smallest single-day return.' },
      { key: 'best_month',            label: 'Best Month',    format: 'percent', tooltip: 'Largest single-month return.' },
      { key: 'worst_month',           label: 'Worst Month',   format: 'percent', tooltip: 'Smallest single-month return.' },
    ],
  },
  {
    key: 'risk_adjusted',
    title: 'Risk-adjusted',
    metrics: [
      { key: 'sharpe_ratio',  label: 'Sharpe',  format: 'ratio', tooltip: 'Excess return per unit of total volatility.' },
      { key: 'sortino_ratio', label: 'Sortino', format: 'ratio', tooltip: 'Excess return per unit of downside volatility.' },
      { key: 'calmar_ratio',  label: 'Calmar',  format: 'ratio', tooltip: 'Excess CAGR over risk-free rate, divided by absolute max drawdown.' },
    ],
  },
  {
    key: 'tail',
    title: 'Tail',
    metrics: [
      { key: 'var_95',   label: 'VaR 95%',  format: 'percent', tooltip: 'Value at Risk (95th percentile).' },
      { key: 'var_99',   label: 'VaR 99%',  format: 'percent', tooltip: 'Value at Risk (99th percentile).' },
      { key: 'cvar_5',   label: 'CVaR 5%',  format: 'percent', tooltip: 'Conditional VaR — average loss in the worst 5%.' },
      { key: 'skewness', label: 'Skew',     format: 'ratio',   tooltip: 'Skewness of the return distribution.' },
      { key: 'kurtosis', label: 'Kurtosis', format: 'ratio',   tooltip: 'Excess kurtosis of the return distribution.' },
    ],
  },
  {
    key: 'drawdown',
    title: 'Drawdown',
    metrics: [
      { key: 'max_drawdown',          label: 'Max DD',     format: 'percent', tooltip: 'Largest peak-to-trough decline.' },
      { key: 'avg_drawdown',          label: 'Avg DD',     format: 'percent', tooltip: 'Average drawdown across all underwater periods.' },
      { key: 'current_drawdown',      label: 'Current DD', format: 'percent', tooltip: 'Drawdown at the most recent observation (always ≤ 0).' },
      { key: 'longest_drawdown_days', label: 'Longest DD', format: 'days',    tooltip: 'Length of the longest underwater stretch.' },
      { key: 'time_underwater_days',  label: 'Underwater', format: 'days',    tooltip: 'Total trading days spent below a prior peak.' },
    ],
  },
];

/**
 * Format a metric value per the contract rules.
 * Returns the string to display and an optional sign class for coloring.
 *
 * @param {*}      value
 * @param {string} format   one of 'ratio' | 'percent' | 'days'
 * @returns {{text: string, signClass: string | null}}
 */
function formatMetric(value, format) {
  if (value == null || !Number.isFinite(value)) {
    return { text: '—', signClass: 'muted' };
  }
  if (format === 'ratio') {
    return { text: value.toFixed(2), signClass: null };
  }
  if (format === 'days') {
    return { text: `${Math.round(value)} days`, signClass: null };
  }
  if (format === 'pct_value') {
    // Value is ALREADY a percent — render as-is, no ×100, unsigned, neutral.
    return { text: `${value.toFixed(2)}%`, signClass: null };
  }
  if (format === 'percent') {
    const pct = value * 100;
    // Build the sign manually so "+0.00%" doesn't slip out (we want 0 to be unsigned).
    const sign = pct > 0 ? '+' : pct < 0 ? '-' : '';
    const text = `${sign}${Math.abs(pct).toFixed(2)}%`;
    let signClass = null;
    if (pct > 0) signClass = 'positive';
    else if (pct < 0) signClass = 'negative';
    return { text, signClass };
  }
  // Unknown format — fall back to raw string. Loud rather than silent.
  return { text: String(value), signClass: null };
}

function MetricRow({ metric, value }) {
  const { text, signClass } = formatMetric(value, metric.format);
  const cls = signClass ? `${styles.metricValue} ${styles[signClass]}` : styles.metricValue;
  return (
    <div className={styles.metricRow}>
      <span className={styles.metricLabel} title={metric.tooltip || metric.label}>
        {metric.label}
      </span>
      <span className={cls}>{text}</span>
    </div>
  );
}

/**
 * Statistics — reusable performance-metrics panel for any equity curve.
 *
 * Renders a single surface-coloured panel with four columns
 * (Return, Risk-adjusted, Tail, Drawdown). Each column stacks its
 * metrics vertically as label-left / value-right rows. The Rf input
 * lives top-right of the header. The component is used identically on
 * every page — callers do not wrap it in a card.
 *
 * Fetches POST /api/statistics with the supplied dates/equity and an
 * editable risk-free rate. Changes to the Rf input debounce 300ms then
 * trigger a refetch. Previous results stay visible during loading and
 * on error.
 *
 * Props:
 *   dates                 {number[]}  YYYYMMDD integers (length == equity)
 *   equity                {number[]}  equity curve values
 *   defaultRiskFreeRate   {number=}   annualized decimal, default 0.04 (4%)
 *   costs                 {{slippagePct: number, feesPct: number}=}
 *                                     total execution costs paid over the run,
 *                                     each ALREADY in percent units. When
 *                                     present, a "Costs" column renders two
 *                                     separate rows (Slippage paid / Fees paid).
 *                                     Absent ⇒ the column is not rendered.
 *                                     These come from the compute response
 *                                     (total_slippage_paid_pct /
 *                                     total_fees_paid_pct), NOT /api/statistics.
 */
export default function Statistics({
  dates,
  equity,
  defaultRiskFreeRate = DEFAULT_RF,
  costs,
}) {
  // Rf is owned by this component — the input shows the percentage form (e.g. "4.00").
  const [rfPct, setRfPct] = useState(() => (defaultRiskFreeRate * 100).toFixed(2));
  const [debouncedRf, setDebouncedRf] = useState(defaultRiskFreeRate);

  const [data, setData] = useState(null);   // last successful statistics suite
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    const handle = setTimeout(() => {
      const parsed = parseFloat(rfPct);
      if (Number.isFinite(parsed)) {
        setDebouncedRf(parsed / 100);
      }
    }, RF_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [rfPct]);

  // Stable JSON hash of dates/equity so we don't refetch when an
  // identical-content array is passed by reference. ``dates`` and
  // ``equity`` are typically large; we accept the JSON cost because the
  // alternative (deep equality each render) is no cheaper and the
  // refetch is far more expensive.
  const inputsKey = useMemo(() => {
    if (!Array.isArray(dates) || !Array.isArray(equity)) return null;
    if (dates.length === 0 || equity.length === 0) return null;
    if (dates.length !== equity.length) return null;
    // Use length + endpoints + checksum-ish marker to avoid huge keys.
    // Full JSON would be safer but expensive; this is a pragmatic tradeoff.
    return `${dates.length}:${dates[0]}:${dates[dates.length - 1]}:${equity[0]}:${equity[equity.length - 1]}`;
  }, [dates, equity]);

  // Track the latest request so out-of-order responses don't overwrite a newer result.
  const reqIdRef = useRef(0);

  useEffect(() => {
    if (inputsKey == null) {
      // Nothing to fetch — clear stale state and bail.
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }
    const myReqId = ++reqIdRef.current;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchStatistics(
      { dates, equity, riskFreeRate: debouncedRf },
      { signal: controller.signal },
    )
      .then((res) => {
        if (reqIdRef.current !== myReqId) return; // stale
        setData(res);
        setLoading(false);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        if (reqIdRef.current !== myReqId) return; // stale
        setError(err && err.message ? err.message : 'Failed to load statistics.');
        setLoading(false);
      });
    return () => {
      controller.abort();
    };
    // `dates`/`equity` are covered by `inputsKey`; we deliberately exclude them.
  }, [inputsKey, debouncedRf]); // eslint-disable-line react-hooks/exhaustive-deps

  const onRfChange = (e) => {
    setRfPct(e.target.value);
  };

  return (
    <div className={styles.panel}>
      <div className={styles.headerBar}>
        <div className={styles.headerLeft}>
          <h3 className={styles.headerTitle}>Statistics</h3>
          {data && Number.isFinite(data.num_observations) && (
            <span className={styles.obsCount}>{data.num_observations} obs</span>
          )}
        </div>
        <RiskFreeRateInput
          valuePct={rfPct}
          onChange={onRfChange}
          ariaLabel="Risk-free rate (annualized, percent)"
          label="Risk-free rate:"
        />
      </div>

      {loading && <div className={styles.statusRow}>Loading…</div>}
      {error && <div className={styles.error} role="alert">{error}</div>}

      <div className={styles.columns}>
        {SECTIONS.map((section) => {
          const group = data && data[section.key];
          return (
            <div key={section.key} className={styles.column}>
              <h4 className={styles.sectionTitle}>{section.title}</h4>
              <div className={styles.metricList}>
                {section.metrics.map((m) => (
                  <MetricRow
                    key={m.key}
                    metric={m}
                    value={group ? group[m.key] : null}
                  />
                ))}
              </div>
            </div>
          );
        })}

        {/* Costs — rendered only when the caller supplies execution costs.
            Slippage and fees ALWAYS show as two separate rows (never merged),
            both already in percent units (see the 'pct_value' formatter). */}
        {costs && (
          <div className={styles.column} data-testid="statistics-costs">
            <h4 className={styles.sectionTitle}>Costs</h4>
            <div className={styles.metricList}>
              <MetricRow
                metric={{
                  key: 'slippage_paid',
                  label: 'Slippage paid',
                  format: 'pct_value',
                  tooltip: 'Total slippage paid over the run, as a percent of capital.',
                }}
                value={costs.slippagePct}
              />
              <MetricRow
                metric={{
                  key: 'fees_paid',
                  label: 'Fees paid',
                  format: 'pct_value',
                  tooltip: 'Total fees paid over the run, as a percent of capital.',
                }}
                value={costs.feesPct}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
