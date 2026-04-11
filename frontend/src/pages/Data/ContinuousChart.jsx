import { useState, useCallback } from 'react';
import Plot from 'react-plotly.js';
import useAsync from '../../hooks/useAsync';
import useTheme from '../../hooks/useTheme';
import { getContinuousSeries, getAvailableCycles } from '../../api/data';
import { buildBaseLayout, CHART_CONFIG, TRACE_COLORS, getChartColors } from '../../utils/chartTheme';
import { formatDateInt } from '../../utils/format';
import styles from './ChartBase.module.css';

function ContinuousChart({ collection }) {
  const theme = useTheme();
  const colors = getChartColors(theme);

  const [adjustment, setAdjustment] = useState('none');
  const [cycle, setCycle] = useState('');
  const [chartType, setChartType] = useState('candlestick');

  const { data: cyclesData } = useAsync(
    () => getAvailableCycles(collection),
    [collection]
  );

  const fetchSeries = useCallback(
    () => getContinuousSeries(collection, {
      strategy: 'front_month',
      adjustment,
      cycle: cycle || undefined,
    }),
    [collection, adjustment, cycle]
  );

  const { data, loading, error } = useAsync(fetchSeries, [collection, adjustment, cycle]);

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

  const dates = data.dates.map(formatDateInt);
  const hasVolume = data.volume && data.volume.some((v) => v > 0);
  const hasOHLC = data.open && data.high && data.low && data.close;
  const rollDates = data.roll_dates || [];

  const traces = [];

  if (chartType === 'candlestick' && hasOHLC) {
    traces.push({
      x: dates,
      open: data.open,
      high: data.high,
      low: data.low,
      close: data.close,
      type: 'candlestick',
      name: 'OHLC',
      increasing: { line: { color: '#10b981' } },
      decreasing: { line: { color: '#ef4444' } },
    });
  } else {
    traces.push({
      x: dates,
      y: data.close,
      type: 'scatter',
      mode: 'lines',
      name: 'Close',
      line: { color: TRACE_COLORS[0], width: 1.5 },
      hovertemplate: '%{x}<br>Close: %{y:,.2f}<extra></extra>',
    });
  }

  if (hasVolume) {
    traces.push({
      x: dates,
      y: data.volume,
      type: 'bar',
      name: 'Volume',
      yaxis: 'y2',
      marker: { color: colors.volumeBar },
      hovertemplate: '%{x}<br>Volume: %{y:,.0f}<extra></extra>',
    });
  }

  const rollShapes = rollDates.map((d) => ({
    type: 'line',
    x0: formatDateInt(d),
    x1: formatDateInt(d),
    y0: 0,
    y1: 1,
    yref: 'paper',
    line: { color: TRACE_COLORS[2], width: 1, dash: 'dash' },
  }));

  const layout = buildBaseLayout({
    xaxis: {
      type: 'date',
      showticklabels: true,
      rangeslider: { visible: false },
      ...(hasVolume ? { anchor: 'y2' } : {}),
    },
    yaxis: {
      title: { text: 'Price', font: { size: 11, color: colors.secondaryFont } },
      domain: hasVolume ? [0.28, 1.0] : [0, 1.0],
      tickformat: ',.0f',
    },
    ...(hasVolume
      ? {
          yaxis2: {
            domain: [0, 0.2],
            zeroline: false,
            showgrid: true,
            title: { text: 'Volume', font: { size: 11, color: colors.secondaryFont } },
            anchor: 'x',
          },
        }
      : {}),
    shapes: rollShapes,
    legend: {
      orientation: 'v',
      x: 0.99,
      xanchor: 'right',
      y: 1.0,
      yanchor: 'top',
      font: { size: 11 },
      bgcolor: colors.legendBg,
      bordercolor: colors.linecolor,
      borderwidth: 1,
    },
    margin: { l: 60, r: 24, t: 12, b: hasVolume ? 40 : 50 },
  }, theme);

  const adjustmentLabels = { none: 'None', proportional: 'Proportional', difference: 'Difference' };

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
      </div>

      <div className={styles.chartWrapper}>
        <Plot
          data={traces}
          layout={layout}
          config={CHART_CONFIG}
          useResizeHandler={true}
          style={{ width: '100%', height: '100%' }}
        />
      </div>
    </div>
  );
}

export default ContinuousChart;
