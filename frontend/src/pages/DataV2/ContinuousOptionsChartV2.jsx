import { useState, useMemo } from 'react';
import { useContinuousOptionsV2 } from '../../hooks/marketQueries';
import Chart from '../../components/Chart';
import { TRACE_COLORS } from '../../utils/chartTheme';
import { formatDateInt } from '../../utils/format';
import baseStyles from '../Data/ChartBase.module.css';
import styles from './DataV2.module.css';

// Sensible default target per criterion: ATM moneyness = 1.0; strike left to
// the user (blank until typed, since strike scale is instrument-specific).
const DEFAULT_TARGET = { strike: '', moneyness: '1.0' };

/**
 * v2-native options continuous builder. Per-date selection over settlement
 * values by **strike** or **moneyness**, **AtExpiry** roll (fixed). Delta-based
 * selection is present but DISABLED/greyed (fact_greeks is empty in v2) with a
 * "greeks unavailable in v2" tooltip. Renders the settlement-value stream via
 * the shared Chart component with sell/buy roll markers.
 *
 * The endpoint returns { points:{ ts, value }, roll_dates, contracts,
 * spot_source? } where ``ts`` and ``roll_dates`` are YYYYMMDD integers (e.g.
 * 20240618). They are converted to YYYY-MM-DD strings via ``formatDateInt``
 * before use — the shared Chart forces ``xaxis.type:'date'``, so raw ints would
 * be read as epoch-ms and land on 1970. ``points.contract`` carries the
 * selected contract code per bar (1:1 with ``ts``), used to label each roll's
 * sell (bar i-1) and buy (bar i) marker with the exact contract active there.
 * The backend drops non-positive settlements, so plotted values are always > 0.
 */
function ContinuousOptionsChartV2({ objectId, symbol }) {
  const [criterion, setCriterion] = useState('strike');
  const [target, setTarget] = useState(DEFAULT_TARGET.strike);
  const [optionType, setOptionType] = useState('put');

  function handleCriterionChange(next) {
    if (next === 'delta') return; // disabled — greeks unavailable in v2
    setCriterion(next);
    setTarget(DEFAULT_TARGET[next] ?? '');
  }

  const { data, loading, error } = useContinuousOptionsV2(objectId, {
    criterion,
    target,
    optionType,
    roll: 'at_expiry',
  });

  // Backend already drops non-positive/NULL settlements, so every plotted value
  // is > 0. Just map the YYYYMMDD int ts to YYYY-MM-DD strings for the date axis.
  const { xs, ys } = useMemo(() => {
    const points = data?.points;
    const ts = points?.ts;
    const raw = points?.value;
    if (!Array.isArray(ts) || !Array.isArray(raw)) return { xs: [], ys: [] };
    return { xs: ts.map(formatDateInt), ys: raw };
  }, [data]);

  const traces = useMemo(() => {
    if (xs.length === 0) return [];
    return [{
      x: xs, y: ys, type: 'scatter', mode: 'lines',
      name: `${optionType === 'call' ? 'Call' : 'Put'} · ${criterion} ${target}`,
      line: { color: TRACE_COLORS[0], width: 1 },
      hovertemplate: '%{x}<br>Settlement: %{y:,.4f}<extra></extra>',
      connectgaps: false,
    }];
  }, [xs, ys, optionType, criterion, target]);

  // Sell+buy roll markers from roll_dates (YYYYMMDD ints) aligned to points.ts.
  // Both roll_dates and xs are converted to YYYY-MM-DD strings so indexOf and
  // the marker x placement stay on the same string date axis as the trace.
  const markers = useMemo(() => {
    const rd = data?.roll_dates;
    // Per-date contract codes (1:1 with points.ts) — label the exact contract
    // active on the sell bar (i-1) and the buy bar (i). Robust to moneyness
    // strike drift within an expiration segment (per-segment would be lossy).
    const contract = data?.points?.contract;
    if (!Array.isArray(rd) || rd.length === 0 || xs.length === 0) return [];
    const out = [];
    for (let k = 0; k < rd.length; k++) {
      const xLabel = formatDateInt(rd[k]);
      const i = xs.indexOf(xLabel);
      if (i <= 0) continue;
      const sell = ys[i - 1];
      const buy = ys[i];
      if (Number.isFinite(sell)) {
        out.push({ x: xLabel, y: sell, kind: 'sell', customdata: [contract?.[i - 1], sell] });
      }
      if (Number.isFinite(buy)) {
        out.push({ x: xLabel, y: buy, kind: 'buy', customdata: [contract?.[i], buy] });
      }
    }
    return out;
  }, [data, xs, ys]);

  const markerHovertemplates = useMemo(() => ({
    sell: '<b>Sell</b><br>%{customdata[0]}<br>Settlement: %{customdata[1]:,.4f}<extra></extra>',
    buy:  '<b>Buy</b><br>%{customdata[0]}<br>Settlement: %{customdata[1]:,.4f}<extra></extra>',
  }), []);

  const pointCount = xs.length;
  const needsTarget = target === '' || target === null || target === undefined;

  return (
    <div className={baseStyles.container} data-testid="continuous-options-v2">
      <div className={baseStyles.header}>
        <h2 className={baseStyles.title}>{symbol} — Continuous Options (v2)</h2>
        {pointCount > 0 && (
          <span className={baseStyles.meta}>
            {pointCount.toLocaleString()} points
            {data?.spot_source ? ` · spot: ${data.spot_source}` : ''}
          </span>
        )}
      </div>

      <div className={baseStyles.controls}>
        <span className={baseStyles.controlLabel} role="radiogroup" aria-label="Selection criterion" style={{ gap: 12 }}>
          Criterion
          <label className={styles.criterionOption}>
            <input
              type="radio" name="v2opt-criterion" value="strike"
              checked={criterion === 'strike'}
              onChange={() => handleCriterionChange('strike')}
            />
            Strike
          </label>
          <label className={styles.criterionOption}>
            <input
              type="radio" name="v2opt-criterion" value="moneyness"
              checked={criterion === 'moneyness'}
              onChange={() => handleCriterionChange('moneyness')}
            />
            Moneyness
          </label>
          <label
            className={`${styles.criterionOption} ${styles.criterionDisabled}`}
            title="greeks unavailable in v2"
          >
            <input type="radio" name="v2opt-criterion" value="delta" disabled aria-disabled="true" />
            Delta
          </label>
        </span>

        <label className={baseStyles.controlLabel}>
          Target
          <input
            type="number"
            className={baseStyles.select}
            style={{ width: '90px' }}
            value={target}
            step={criterion === 'moneyness' ? 0.05 : 5}
            placeholder={criterion === 'moneyness' ? '1.0' : 'strike'}
            onChange={(e) => setTarget(e.target.value)}
          />
        </label>

        <label className={baseStyles.controlLabel}>
          Option type
          <select className={baseStyles.select} value={optionType} onChange={(e) => setOptionType(e.target.value)}>
            <option value="call">Call</option>
            <option value="put">Put</option>
          </select>
        </label>

        <label className={baseStyles.controlLabel}>
          Roll
          <select className={baseStyles.select} value="at_expiry" disabled title="v2 options roll at expiry">
            <option value="at_expiry">At expiry</option>
          </select>
        </label>
      </div>

      {needsTarget && (
        <div className={baseStyles.snapNotice} role="status">
          Enter a {criterion} target above to build the continuous settlement stream.
        </div>
      )}

      {error && (
        <div className={baseStyles.error} data-testid="continuous-options-v2-error">
          {error.message || String(error)}
        </div>
      )}

      {loading && (
        <div className={baseStyles.status}>Loading continuous options series…</div>
      )}

      {!loading && !error && !needsTarget && pointCount === 0 && (
        <div className={baseStyles.status}>No data returned for this selection.</div>
      )}

      {!loading && !error && pointCount > 0 && (
        <div className={baseStyles.chartCard}>
          <Chart
            traces={traces}
            markers={markers}
            markerHovertemplates={markerHovertemplates}
            className={baseStyles.chartWrapper}
            downloadFilename={`${symbol}-v2-continuous-options-${criterion}-${target}-${optionType}`}
          />
        </div>
      )}
    </div>
  );
}

export default ContinuousOptionsChartV2;
