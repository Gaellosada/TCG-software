import { useState, useEffect, useMemo } from 'react';
import useAsync from '../../hooks/useAsync';
import useTheme from '../../hooks/useTheme';
import useChartPreference from '../../hooks/useChartPreference';
import Chart from '../../components/Chart';
import { getInstrumentPrices } from '../../api/data';
import { TRACE_COLORS, getChartColors } from '../../utils/chartTheme';
import { prepareChartData } from '../../utils/ohlcHelpers';
import { formatDateInt } from '../../utils/format';
import styles from './ChartBase.module.css';

function PriceChart({ collection, instrument }) {
  const theme = useTheme();
  const colors = getChartColors(theme);
  const preference = useChartPreference();
  const [chartType, setChartType] = useState(preference);

  // Sync local state when global preference changes
  useEffect(() => {
    setChartType(preference);
  }, [preference]);

  const { data, loading, error } = useAsync(
    () => getInstrumentPrices(collection, instrument),
    [collection, instrument]
  );

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
        x: dates,
        open: prepared.open,
        high: prepared.high,
        low: prepared.low,
        close: prepared.close,
        type: 'candlestick',
        name: 'OHLC',
        increasing: { line: { color: '#10b981' } },
        decreasing: { line: { color: '#ef4444' } },
      });
    } else {
      t.push({
        x: dates,
        y: data.close,
        type: 'scatter',
        mode: 'lines',
        name: 'Close',
        line: { color: TRACE_COLORS[0], width: 1 },
        hovertemplate: '%{x}<br>Close: %{y:,.2f}<extra></extra>',
      });
    }

    if (prepared.hasVolume) {
      t.push({
        x: dates,
        y: data.volume,
        type: 'bar',
        name: 'Volume',
        yaxis: 'y2',
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
      ...(prepared.hasVolume
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
    };

    return { traces: t, layoutOverrides: lo, hasOHLC: prepared.hasOHLC };
  }, [data, chartType, colors]);

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

      <div className={styles.chartCard}>
        <Chart
          traces={traces}
          layoutOverrides={layoutOverrides}
          className={styles.chartWrapper}
        />
      </div>
    </div>
  );
}

export default PriceChart;
