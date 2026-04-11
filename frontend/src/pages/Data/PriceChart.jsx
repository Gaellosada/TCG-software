import { useState } from 'react';
import Plot from 'react-plotly.js';
import useAsync from '../../hooks/useAsync';
import useTheme from '../../hooks/useTheme';
import { getInstrumentPrices } from '../../api/data';
import { buildBaseLayout, CHART_CONFIG, TRACE_COLORS, getChartColors } from '../../utils/chartTheme';
import { formatDateInt } from '../../utils/format';
import styles from './ChartBase.module.css';

function PriceChart({ collection, instrument }) {
  const theme = useTheme();
  const colors = getChartColors(theme);
  const [chartType, setChartType] = useState('candlestick');

  const { data, loading, error } = useAsync(
    () => getInstrumentPrices(collection, instrument),
    [collection, instrument]
  );

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.status}>Loading price data...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.container}>
        <div className={styles.error}>Failed to load prices: {error.message}</div>
      </div>
    );
  }

  if (!data || !data.dates || data.dates.length === 0) {
    return (
      <div className={styles.container}>
        <div className={styles.status}>No price data available.</div>
      </div>
    );
  }

  const dates = data.dates.map(formatDateInt);
  const hasVolume = data.volume && data.volume.some((v) => v > 0);
  const hasOHLC = data.open && data.high && data.low && data.close;

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

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>{instrument}</h2>
        <span className={styles.meta}>
          {data.dates.length.toLocaleString()} bars
          &nbsp;&middot;&nbsp;
          {formatDateInt(data.dates[0])} to {formatDateInt(data.dates[data.dates.length - 1])}
        </span>
      </div>

      {hasOHLC && (
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
        </div>
      )}

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

export default PriceChart;
