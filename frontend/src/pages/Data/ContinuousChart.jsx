import { useState, useCallback, useEffect, useMemo } from 'react';
import useAsync from '../../hooks/useAsync';
import useTheme from '../../hooks/useTheme';
import useChartPreference from '../../hooks/useChartPreference';
import useProviderPreference from '../../hooks/useProviderPreference';
import Chart from '../../components/Chart';
import PillToggle from '../../components/PillToggle';
import { getContinuousSeries, getAvailableCycles } from '../../api/data';
import { TRACE_COLORS, getChartColors, createVerticalLineTrace, hiddenOverlayAxis } from '../../utils/chartTheme';
import { prepareChartData } from '../../utils/ohlcHelpers';
import { formatDateInt } from '../../utils/format';
import styles from './ChartBase.module.css';

function ContinuousChart({ collection }) {
  const theme = useTheme();
  const colors = getChartColors(theme);
  const preference = useChartPreference();
  const [selectedProvider, setSelectedProvider] = useState(null);
  const { getDefault } = useProviderPreference();

  const [adjustment, setAdjustment] = useState('none');
  const [cycle, setCycle] = useState('');
  const [chartType, setChartType] = useState(preference);

  // Sync local state when global preference changes
  useEffect(() => {
    setChartType(preference);
  }, [preference]);

  // Reset provider selection when collection changes
  useEffect(() => {
    setSelectedProvider(null);
  }, [collection]);

  const providerParam = selectedProvider || getDefault(collection) || undefined;

  const { data: cyclesData } = useAsync(
    () => getAvailableCycles(collection),
    [collection]
  );

  const fetchSeries = useCallback(
    () => getContinuousSeries(collection, {
      strategy: 'front_month',
      adjustment,
      cycle: cycle || undefined,
      provider: providerParam,
    }),
    [collection, adjustment, cycle, providerParam]
  );

  const { data, loading, error } = useAsync(fetchSeries, [collection, adjustment, cycle, providerParam]);

  const rollDates = (data && data.roll_dates) || [];

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

    if (rollDates.length > 0) {
      t.push(createVerticalLineTrace(
        rollDates.map(formatDateInt),
        { name: 'Roll', color: 'rgba(160, 160, 160, 0.35)', dash: 'dot', yaxisKey: 'y3' },
      ));
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
      yaxis3: hiddenOverlayAxis(),
    };

    return { traces: t, layoutOverrides: lo, hasOHLC: prepared.hasOHLC };
  }, [data, rollDates, chartType, colors]);

  const adjustmentLabels = { none: 'None', proportional: 'Proportional', difference: 'Difference' };

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

        {data?.available_providers?.length > 1 && (
          <PillToggle
            options={data.available_providers.map(p => ({ value: p, label: p }))}
            value={data.provider}
            onChange={setSelectedProvider}
            ariaLabel="Data provider"
          />
        )}
      </div>

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

export default ContinuousChart;
