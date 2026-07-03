import { useState, useEffect, useMemo } from 'react';
import { useContinuousSeries, useAvailableCycles } from '../../hooks/marketQueries';
import useTheme from '../../hooks/useTheme';
import useChartPreference from '../../hooks/useChartPreference';
import Chart from '../../components/Chart';
import { TRACE_COLORS, getChartColors } from '../../utils/chartTheme';
import { prepareChartData } from '../../utils/ohlcHelpers';
import { formatDateInt } from '../../utils/format';
import styles from './ChartBase.module.css';

function ContinuousChart({ collection }) {
  const theme = useTheme();
  const colors = getChartColors(theme);
  const preference = useChartPreference();

  const [adjustment, setAdjustment] = useState('none');
  const [cycle, setCycle] = useState('');
  const [rollOffset, setRollOffset] = useState(2);
  // Roll strategy (Issue #3): 'front_month' (default) or 'end_of_month'.
  const [strategy, setStrategy] = useState('front_month');
  const [chartType, setChartType] = useState(preference);

  // Sync local state when global preference changes
  useEffect(() => {
    setChartType(preference);
  }, [preference]);

  // Reset cycle when collection changes — a cycle selected for one
  // futures product is rarely valid for another.
  useEffect(() => {
    setCycle('');
  }, [collection]);

  // SWR: cached cycles + rolled series render instantly on re-navigation.
  // The continuous query keeps the previous series on screen while a new
  // adjustment/cycle/roll-offset loads (placeholderData: keepPreviousData),
  // so changing a control never flashes a loading state.
  const { data: cyclesData } = useAvailableCycles(collection);

  const { data, loading, error } = useContinuousSeries(collection, {
    strategy,
    adjustment,
    cycle,
    rollOffset,
  });

  const rollDates = (data && data.roll_dates) || [];

  // Per-roll marker overlay (sell + buy dot at the roll boundary).
  //
  // Derived from the existing endpoint payload (`roll_dates`, `contracts`,
  // `dates`, `close`) — see CONTRACT §C.2. The sell Y is the OLD contract's
  // last close (`close[i-1]`); the buy Y is the NEW contract's first close
  // (`close[i]`) where `i = dates.indexOf(roll_dates[k])`. Adjustment math is
  // already baked into `close` by the backend — no mode branch needed:
  //   - `none`        → close[i-1] ≠ close[i] typically (visible vertical gap)
  //   - `ratio`/`difference` → close[i-1] == close[i] (overlap → ring-on-dot)
  //
  // Edge cases (all skip silently — first-bar roll is a backend bug, missing
  // date implies start/end trimmed the data, null/NaN closes are pathological):
  //   - roll_dates empty → markers = []
  //   - dates.indexOf(rollDate) === -1 → skip
  //   - i === 0 → skip (no predecessor close for sell-side)
  //   - close[i-1] or close[i] null/NaN → skip
  //
  // x uses `formatDateInt` so markers align with the price-line trace's x axis.
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
      const oldContract = contracts[k];
      const newContract = contracts[k + 1];
      const xLabel = formatDateInt(rollDateInt);
      out.push({
        x: xLabel, y: sellPrice, kind: 'sell',
        customdata: [oldContract, sellPrice],
      });
      out.push({
        x: xLabel, y: buyPrice, kind: 'buy',
        customdata: [newContract, buyPrice],
      });
    }
    return out;
  }, [data]);

  // Futures-shaped hovertemplates — sparser than the options default
  // (no strike/type). customdata[0] = contract id, customdata[1] = price.
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
      xaxis: {
        ...(prepared.hasVolume ? { anchor: 'y2' } : {}),
      },
      yaxis: {
        title: { text: 'Price', font: { size: 11, color: colors.secondaryFont } },
        domain: prepared.hasVolume ? [0.28, 1.0] : [0, 1.0],
      },
      ...(prepared.hasVolume ? {
        yaxis2: { domain: [0, 0.2], zeroline: false, showgrid: true,
          title: { text: 'Volume', font: { size: 11, color: colors.secondaryFont } }, anchor: 'x' },
      } : {}),
    };

    return { traces: t, layoutOverrides: lo, hasOHLC: prepared.hasOHLC };
  }, [data, chartType, colors]);

  const adjustmentLabels = { none: 'None', ratio: 'Ratio', difference: 'Difference' };

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.status}>Loading continuous series...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.container}>
        <div className={styles.error}>Failed to load series: {error.message}</div>
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
        <h2 className={styles.title}>{collection} — Continuous</h2>
        <span className={styles.meta}>
          {data.dates.length.toLocaleString()} bars
          &nbsp;&middot;&nbsp;
          {formatDateInt(data.dates[0])} to {formatDateInt(data.dates[data.dates.length - 1])}
          {rollDates.length > 0 && (
            <>
              &nbsp;&middot;&nbsp;
              {rollDates.length} roll{rollDates.length !== 1 ? 's' : ''}
            </>
          )}
          {data.contracts?.length > 0 && (
            <>
              &nbsp;&middot;&nbsp;
              {data.contracts.length} contract{data.contracts.length !== 1 ? 's' : ''}
            </>
          )}
        </span>
      </div>

      <div className={styles.controls}>
        {hasOHLC && (
          <label className={styles.controlLabel}>
            Chart
            <select
              className={styles.select}
              value={chartType}
              onChange={(e) => setChartType(e.target.value)}
            >
              <option value="candlestick">Candlestick</option>
              <option value="line">Line</option>
            </select>
          </label>
        )}

        <label className={styles.controlLabel}>
          Roll strategy
          <select
            className={styles.select}
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
          >
            <option value="front_month">Front month (at expiry)</option>
            <option value="end_of_month">End of month</option>
          </select>
        </label>

        <label className={styles.controlLabel}>
          Adjustment
          <select
            className={styles.select}
            value={adjustment}
            onChange={(e) => setAdjustment(e.target.value)}
          >
            {Object.entries(adjustmentLabels).map(([val, label]) => (
              <option key={val} value={val}>{label}</option>
            ))}
          </select>
        </label>

        <label className={styles.controlLabel}>
          Cycle
          <select
            className={styles.select}
            value={cycle}
            onChange={(e) => setCycle(e.target.value)}
          >
            <option value="">All</option>
            {cyclesData && cyclesData.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </label>

        <label className={styles.controlLabel}>
          Roll Offset (days)
          <input
            type="number"
            className={styles.select}
            style={{ width: '56px' }}
            value={rollOffset}
            min={0}
            max={30}
            onChange={(e) => setRollOffset(Math.max(0, Math.min(30, parseInt(e.target.value, 10) || 0)))}
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
          downloadFilename={`${collection}-continuous-${adjustment}${cycle ? `-${cycle}` : ''}`}
        />
      </div>
    </div>
  );
}

export default ContinuousChart;
