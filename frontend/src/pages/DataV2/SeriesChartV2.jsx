import { useState, useEffect, useMemo } from 'react';
import { useSeriesV2 } from '../../hooks/marketQueries';
import useTheme from '../../hooks/useTheme';
import useChartPreference from '../../hooks/useChartPreference';
import Chart from '../../components/Chart';
import { TRACE_COLORS, getChartColors } from '../../utils/chartTheme';
import { formatDateInt } from '../../utils/format';
import styles from '../Data/ChartBase.module.css';

/**
 * Chart a single v2 series, dispatching the field set on ``serie.type``:
 *   - bar    → candlestick / line (OHLC) + volume (on y2)
 *   - value  → single line
 *   - greeks → one line per greek field (delta/gamma/theta/vega/rho/iv)
 *   - bbba   → one line per best_bid/ask value field
 *   - (fallback) → one line per numeric field present in ``fields``
 *
 * The response carries ``points: { ts:[...], <field>:[...] }`` where ``ts`` are
 * YYYYMMDD integers (e.g. 20240618). They are converted to YYYY-MM-DD strings
 * via ``formatDateInt`` before being used as the Plotly x axis — the shared
 * Chart forces ``xaxis.type:'date'``, so raw ints would be read as epoch-ms and
 * collapse the whole series onto 1970. All rendering goes through the shared
 * Chart component (no forked v2 chart).
 */
const OHLC_FIELDS = ['open', 'high', 'low', 'close'];

function SeriesChartV2({ serieId, serieType, label, downloadFilename }) {
  const theme = useTheme();
  const colors = getChartColors(theme);
  const preference = useChartPreference();
  const [chartType, setChartType] = useState(preference);

  useEffect(() => { setChartType(preference); }, [preference]);

  const { data, loading, error } = useSeriesV2(serieId);

  const { traces, layoutOverrides, hasOHLC, pointCount } = useMemo(() => {
    const points = data?.points;
    const ts = points?.ts;
    if (!points || !Array.isArray(ts) || ts.length === 0) {
      return { traces: [], layoutOverrides: {}, hasOHLC: false, pointCount: 0 };
    }

    const type = data.type || serieType;
    const fields = Array.isArray(data.fields) ? data.fields : Object.keys(points).filter((k) => k !== 'ts');
    // ts are YYYYMMDD ints — convert to YYYY-MM-DD strings for the date x axis.
    const x = ts.map(formatDateInt);
    const t = [];

    // ── bar: OHLC (candlestick/line) + optional volume ──
    if (type === 'bar' && OHLC_FIELDS.every((f) => Array.isArray(points[f]))) {
      const effectiveType = chartType === 'candlestick' ? 'candlestick' : 'line';
      if (effectiveType === 'candlestick') {
        t.push({
          x, open: points.open, high: points.high, low: points.low, close: points.close,
          type: 'candlestick', name: 'OHLC',
          increasing: { line: { color: '#10b981' } },
          decreasing: { line: { color: '#ef4444' } },
        });
      } else {
        t.push({
          x, y: points.close, type: 'scatter', mode: 'lines', name: 'Close',
          line: { color: TRACE_COLORS[0], width: 1 },
          hovertemplate: '%{x}<br>Close: %{y:,.2f}<extra></extra>',
        });
      }

      const hasVolume = Array.isArray(points.volume) && points.volume.some((v) => Number.isFinite(v) && v > 0);
      if (hasVolume) {
        t.push({
          x, y: points.volume, type: 'bar', name: 'Volume', yaxis: 'y2',
          marker: { color: colors.volumeBar },
          hovertemplate: '%{x}<br>Volume: %{y:,.0f}<extra></extra>',
        });
      }

      const lo = {
        xaxis: { ...(hasVolume ? { anchor: 'y2' } : {}) },
        yaxis: {
          title: { text: 'Price', font: { size: 11, color: colors.secondaryFont } },
          domain: hasVolume ? [0.28, 1.0] : [0, 1.0],
        },
        ...(hasVolume ? {
          yaxis2: {
            domain: [0, 0.2], zeroline: false, showgrid: true,
            title: { text: 'Volume', font: { size: 11, color: colors.secondaryFont } }, anchor: 'x',
          },
        } : {}),
      };
      return { traces: t, layoutOverrides: lo, hasOHLC: true, pointCount: ts.length };
    }

    // ── value: single line ──
    if (type === 'value' && Array.isArray(points.value)) {
      t.push({
        x, y: points.value, type: 'scatter', mode: 'lines', name: label || 'Value',
        line: { color: TRACE_COLORS[0], width: 1 },
        hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
        connectgaps: false,
      });
      return { traces: t, layoutOverrides: {}, hasOHLC: false, pointCount: ts.length };
    }

    // ── greeks / bbba / fallback: one line per numeric field ──
    let colorIdx = 0;
    for (const f of fields) {
      if (f === 'ts') continue;
      const arr = points[f];
      if (!Array.isArray(arr)) continue;
      t.push({
        x, y: arr, type: 'scatter', mode: 'lines', name: f,
        line: { color: TRACE_COLORS[colorIdx % TRACE_COLORS.length], width: 1 },
        hovertemplate: `%{x}<br>${f}: %{y:,.4f}<extra></extra>`,
        connectgaps: false,
      });
      colorIdx++;
    }
    return { traces: t, layoutOverrides: {}, hasOHLC: false, pointCount: ts.length };
  }, [data, serieType, chartType, colors, label]);

  if (loading) {
    return (
      <div className={styles.container}>
        <div className={styles.status}>Loading series…</div>
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
  if (pointCount === 0 || traces.length === 0) {
    return (
      <div className={styles.container}>
        <div className={styles.status}>
          No data for this series (fact table may be empty in v2 — e.g. greeks / bbba).
        </div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>{label || `Series ${serieId}`}</h2>
        <span className={styles.meta}>
          {pointCount.toLocaleString()} points · type {data?.type || serieType}
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
          downloadFilename={downloadFilename || `series-${serieId}`}
        />
      </div>
    </div>
  );
}

export default SeriesChartV2;
