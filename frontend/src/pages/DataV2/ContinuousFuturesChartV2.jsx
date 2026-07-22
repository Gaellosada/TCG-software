import { useState, useEffect, useMemo } from 'react';
import { useContinuousFuturesV2, useV2FuturesCycles } from '../../hooks/marketQueries';
import useTheme from '../../hooks/useTheme';
import useChartPreference from '../../hooks/useChartPreference';
import Chart from '../../components/Chart';
import { TRACE_COLORS, getChartColors } from '../../utils/chartTheme';
import { prepareChartData } from '../../utils/ohlcHelpers';
import { formatDateInt } from '../../utils/format';
import styles from '../Data/ChartBase.module.css';

/**
 * v2 futures continuous builder. Parallel to the v1 ContinuousChart (v1 tree is
 * left untouched per the "new parallel v2 stack" decision), but reads the v2
 * endpoint via ``useContinuousFuturesV2`` / ``useV2FuturesCycles``. Same
 * strategy / adjustment / cycle / roll-offset / rank controls, same roll-marker
 * overlay, rendered through the shared Chart component.
 *
 * NOTE (reviewer): assumes the v2 endpoint returns the v1 continuous shape —
 * integer ``dates`` (YYYYMMDD), ``open/high/low/close/volume`` arrays,
 * ``roll_dates`` (ints), ``contracts``. Confirm against Worker A's response.
 */
function ContinuousFuturesChartV2({ objectId, symbol }) {
  const theme = useTheme();
  const colors = getChartColors(theme);
  const preference = useChartPreference();

  const [adjustment, setAdjustment] = useState('none');
  const [cycle, setCycle] = useState('');
  const [rollOffset, setRollOffset] = useState(2);
  const [strategy, setStrategy] = useState('front_month');
  const [rank, setRank] = useState(3);
  const [chartType, setChartType] = useState(preference);

  useEffect(() => { setChartType(preference); }, [preference]);
  useEffect(() => { setCycle(''); }, [objectId]);

  const { data: cyclesData } = useV2FuturesCycles(objectId);

  const { data, loading, error } = useContinuousFuturesV2(objectId, {
    strategy,
    adjustment,
    cycle,
    rollOffset,
    rank: strategy === 'nth_nearest' ? rank : 1,
  });

  const rollDates = (data && data.roll_dates) || [];

  // Per-roll sell+buy marker overlay (same derivation as v1 ContinuousChart).
  const markers = useMemo(() => {
    if (!data || !data.roll_dates?.length) return [];
    const { roll_dates, contracts, dates, close } = data;
    const out = [];
    for (let k = 0; k < roll_dates.length; k++) {
      const rollDateInt = roll_dates[k];
      const i = dates.indexOf(rollDateInt);
      if (i <= 0) continue;
      const sellPrice = close[i - 1];
      const buyPrice = close[i];
      if (!Number.isFinite(sellPrice) || !Number.isFinite(buyPrice)) continue;
      const xLabel = formatDateInt(rollDateInt);
      out.push({ x: xLabel, y: sellPrice, kind: 'sell', customdata: [contracts[k], sellPrice] });
      out.push({ x: xLabel, y: buyPrice, kind: 'buy', customdata: [contracts[k + 1], buyPrice] });
    }
    return out;
  }, [data]);

  const markerHovertemplates = useMemo(() => ({
    sell: '<b>Sell</b><br>%{customdata[0]}<br>Close: %{customdata[1]:,.2f}<extra></extra>',
    buy:  '<b>Buy</b><br>%{customdata[0]}<br>Close: %{customdata[1]:,.2f}<extra></extra>',
  }), []);

  const { traces, layoutOverrides, hasOHLC } = useMemo(() => {
    if (!data || !data.dates || data.dates.length === 0) {
      return { traces: [], layoutOverrides: {}, hasOHLC: false };
    }
    const dates = data.dates.map(formatDateInt);
    const prepared = prepareChartData(data);
    const effectiveType = prepared.hasOHLC ? chartType : 'line';
    const t = [];

    if (effectiveType === 'candlestick') {
      t.push({
        x: dates, open: prepared.open, high: prepared.high, low: prepared.low, close: prepared.close,
        type: 'candlestick', name: 'OHLC',
        increasing: { line: { color: '#10b981' } }, decreasing: { line: { color: '#ef4444' } },
      });
    } else {
      t.push({
        x: dates, y: data.close, type: 'scatter', mode: 'lines', name: 'Close',
        line: { color: TRACE_COLORS[0], width: 1 },
        hovertemplate: '%{x}<br>Close: %{y:,.2f}<extra></extra>',
      });
    }

    if (prepared.hasVolume) {
      t.push({
        x: dates, y: data.volume, type: 'bar', name: 'Volume', yaxis: 'y2',
        marker: { color: colors.volumeBar },
        hovertemplate: '%{x}<br>Volume: %{y:,.0f}<extra></extra>',
      });
    }

    const lo = {
      xaxis: { ...(prepared.hasVolume ? { anchor: 'y2' } : {}) },
      yaxis: {
        title: { text: 'Price', font: { size: 11, color: colors.secondaryFont } },
        domain: prepared.hasVolume ? [0.28, 1.0] : [0, 1.0],
      },
      ...(prepared.hasVolume ? {
        yaxis2: {
          domain: [0, 0.2], zeroline: false, showgrid: true,
          title: { text: 'Volume', font: { size: 11, color: colors.secondaryFont } }, anchor: 'x',
        },
      } : {}),
    };
    return { traces: t, layoutOverrides: lo, hasOHLC: prepared.hasOHLC };
  }, [data, chartType, colors]);

  const adjustmentLabels = { none: 'None', ratio: 'Ratio', difference: 'Difference' };

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.status}>Loading continuous series…</div>
      </div>
    );
  }
  if (error) {
    return (
      <div className={styles.container}>
        <div className={styles.error}>Failed to load series: {error.message || String(error)}</div>
      </div>
    );
  }
  if (!data || !data.dates || data.dates.length === 0) {
    return (
      <div className={styles.container}>
        <div className={styles.status}>No continuous series data available.</div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>{symbol} — Continuous (v2)</h2>
        <span className={styles.meta}>
          {data.dates.length.toLocaleString()} bars
          &nbsp;&middot;&nbsp;
          {formatDateInt(data.dates[0])} to {formatDateInt(data.dates[data.dates.length - 1])}
          {rollDates.length > 0 && (
            <>&nbsp;&middot;&nbsp;{rollDates.length} roll{rollDates.length !== 1 ? 's' : ''}</>
          )}
          {data.contracts?.length > 0 && (
            <>&nbsp;&middot;&nbsp;{data.contracts.length} contract{data.contracts.length !== 1 ? 's' : ''}</>
          )}
        </span>
      </div>

      <div className={styles.controls}>
        {hasOHLC && (
          <label className={styles.controlLabel}>
            Chart
            <select className={styles.select} value={chartType} onChange={(e) => setChartType(e.target.value)}>
              <option value="candlestick">Candlestick</option>
              <option value="line">Line</option>
            </select>
          </label>
        )}

        <label className={styles.controlLabel}>
          Roll strategy
          <select className={styles.select} value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            <option value="front_month">Front month (at expiry)</option>
            <option value="end_of_month">End of month</option>
            <option value="nth_nearest">Nth-nearest</option>
          </select>
        </label>

        {strategy === 'nth_nearest' && (
          <label className={styles.controlLabel}>
            Rank (Nth contract)
            <input
              type="number" className={styles.select} style={{ width: '56px' }}
              value={rank} min={1} max={12}
              onChange={(e) => setRank(Math.max(1, Math.min(12, parseInt(e.target.value, 10) || 1)))}
            />
          </label>
        )}

        <label className={styles.controlLabel}>
          Adjustment
          <select className={styles.select} value={adjustment} onChange={(e) => setAdjustment(e.target.value)}>
            {Object.entries(adjustmentLabels).map(([val, l]) => (
              <option key={val} value={val}>{l}</option>
            ))}
          </select>
        </label>

        <label className={styles.controlLabel}>
          Cycle
          <select className={styles.select} value={cycle} onChange={(e) => setCycle(e.target.value)}>
            <option value="">All</option>
            {cyclesData && cyclesData.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </label>

        <label className={styles.controlLabel}>
          Roll Offset (days)
          <input
            type="number" className={styles.select} style={{ width: '56px' }}
            value={rollOffset} min={0} max={365}
            onChange={(e) => setRollOffset(Math.max(0, Math.min(365, parseInt(e.target.value, 10) || 0)))}
          />
        </label>
      </div>

      <div className={styles.chartCard}>
        <Chart
          traces={traces}
          markers={markers}
          markerHovertemplates={markerHovertemplates}
          layoutOverrides={layoutOverrides}
          className={styles.chartWrapper}
          downloadFilename={`${symbol}-v2-continuous-${adjustment}${cycle ? `-${cycle}` : ''}`}
        />
      </div>
    </div>
  );
}

export default ContinuousFuturesChartV2;
