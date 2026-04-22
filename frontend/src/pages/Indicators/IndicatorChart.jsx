import { useMemo } from 'react';
import Chart from '../../components/Chart';
import { TRACE_COLORS } from '../../utils/chartTheme';
import { HEADINGS } from './errorTaxonomy';
import ErrorCard from '../../components/ErrorCard/ErrorCard';
import styles from './IndicatorChart.module.css';

// Icon SVG paths per error kind for the indicator chart's error card.
// Preserved byte-for-byte from the pre-refactor inline implementation.
const INDICATOR_ERROR_ICONS = {
  validation: 'M12 9v4M12 17h.01M4 19h16a2 2 0 0 0 1.7-3L13.7 4a2 2 0 0 0-3.4 0L2.3 16A2 2 0 0 0 4 19z',
  runtime: 'M12 9v4M12 17h.01M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0z',
  data: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 8v4l3 2',
  network: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 8v4l3 2',
  offline: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 8v4l3 2',
  generic: 'M12 9v4M12 17h.01M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0z',
};

// Reconcile unknown error_type values to the 'generic' styling bucket
// so the data-error-type attribute reflects the applied CSS variant.
function coerceIndicatorErrorType(errorType) {
  return HEADINGS[errorType] ? errorType : 'generic';
}

/**
 * Bottom panel — one Plotly chart (overlay or stacked-subplot) or an
 * error card, depending on state.
 *
 * Layout modes (driven by ``indicator.ownPanel``):
 *   - ``ownPanel === false`` (default): price + indicator overlaid on a
 *     single y-axis. Retains the Y2 heuristic so small-magnitude indicators
 *     (RSI-ish things) get a secondary right-side axis instead of being
 *     squashed against large prices.
 *   - ``ownPanel === true``: stacked subplots inside a SINGLE Chart —
 *     price occupies the top ~half of the plot area (yaxis), indicator
 *     occupies the bottom ~half (yaxis2). Both share the same x-axis
 *     (via ``yaxis2.anchor = 'x'`` + ``xaxis.anchor = 'y2'``) so zoom/
 *     pan on one pane propagates to the other and hover is unified.
 *     Mirrors the Data page's price/volume stacked layout.
 */
function IndicatorChart({ indicator, result, loading, error }) {
  const ownPanel = !!indicator?.ownPanel;

  const { traces, layoutOverrides, hasData } = useMemo(() => {
    if (!result || !result.dates || result.dates.length === 0) {
      return { traces: [], layoutOverrides: {}, hasData: false };
    }

    const dates = result.dates;
    const seriesTraces = (result.series || []).map((s, i) => ({
      x: dates,
      y: s.close,
      type: 'scatter',
      mode: 'lines',
      name: s.label || `${s.collection}/${s.instrument_id}`,
      line: { color: TRACE_COLORS[i % TRACE_COLORS.length], width: 1 },
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      connectgaps: false,
    }));

    // Rendering mode — default to a continuous line. Indicators that emit
    // sparse outputs (e.g. swing-pivots, engulfment-pattern) rely on
    // ``connectgaps: true`` below so NaN gaps between isolated non-NaN
    // points are bridged — the indicator renders as a zigzag line that
    // visually connects consecutive pivots / breakouts across the NaN
    // bars between them. Without connectgaps a sparse output would draw
    // no line segments and be invisible.
    //
    // ``chartMode`` is honoured as an author hint (registry-only — not
    // round-tripped through user state) but no fancy marker styling is
    // applied for 'markers'; Plotly's defaults are fine.
    const chartMode = indicator?.chartMode || 'lines';
    const INDICATOR_COLOR = '#f59e0b';
    const baseIndTrace = {
      x: dates,
      y: result.indicator,
      type: 'scatter',
      mode: chartMode,
      name: indicator?.name || 'Indicator',
      line: { color: INDICATOR_COLOR, width: 1 },
      marker: { color: INDICATOR_COLOR, size: 6 },
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      // Bridge NaN gaps on the indicator trace so sparse-output indicators
      // (swing-pivots, engulfment-pattern) render as a visible zigzag line
      // connecting consecutive non-NaN points. Price trace keeps
      // connectgaps=false (actual missing data must remain as gaps).
      connectgaps: true,
    };

    if (ownPanel) {
      // Stacked subplots in a single chart. Price top, indicator bottom;
      // x-axis anchored under the bottom pane so the shared axis ticks
      // sit below everything. 4% gap between panes keeps the divider
      // readable without wasting vertical space.
      const indTrace = { ...baseIndTrace, yaxis: 'y2' };
      const lo = {
        xaxis: { anchor: 'y2' },
        yaxis: {
          title: { text: 'Price', font: { size: 11 } },
          domain: [0.52, 1.0],
        },
        yaxis2: {
          title: { text: 'Indicator', font: { size: 11 } },
          domain: [0, 0.48],
          anchor: 'x',
        },
        showlegend: true,
      };
      return {
        traces: [...seriesTraces, indTrace],
        layoutOverrides: lo,
        hasData: true,
      };
    }

    // Overlay mode — retain the Y2 heuristic so small-magnitude
    // indicators get a right-side axis overlaying price. Reduce-based
    // max avoids the spread-into-Math.max stack overflow on long series.
    const absMax = (arr) => {
      let m = 0;
      for (const v of arr) {
        if (v !== null && Number.isFinite(v)) {
          const a = Math.abs(v);
          if (a > m) m = a;
        }
      }
      return m;
    };
    let priceAbsMax = 0;
    for (const s of result.series || []) {
      const m = absMax(s.close || []);
      if (m > priceAbsMax) priceAbsMax = m;
    }
    const indAbsMax = absMax(result.indicator || []);
    const useY2 = indAbsMax < 10 && priceAbsMax > 100;

    const overlayIndTrace = useY2 ? { ...baseIndTrace, yaxis: 'y2' } : baseIndTrace;
    const lo = {
      yaxis: { title: { text: 'Price', font: { size: 11 } } },
      ...(useY2
        ? {
            yaxis2: {
              title: { text: 'Indicator', font: { size: 11 } },
              overlaying: 'y',
              side: 'right',
              showgrid: false,
            },
          }
        : {}),
      showlegend: true,
      legend: { orientation: 'h', y: -0.15 },
    };

    return {
      traces: [...seriesTraces, overlayIndTrace],
      layoutOverrides: lo,
      hasData: true,
    };
  }, [result, indicator?.name, ownPanel]);

  if (loading) {
    return (
      <div className={styles.panel}>
        <div className={styles.state}>Computing...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.panel}>
        <ErrorCard
          error={error}
          headings={HEADINGS}
          fallbackHeading="Error running indicator"
          icons={INDICATOR_ERROR_ICONS}
          styles={styles}
          coerceErrorType={coerceIndicatorErrorType}
        />
      </div>
    );
  }

  if (!hasData) {
    return (
      <div className={styles.panel}>
        <div className={styles.state}>
          {indicator ? 'Run to see chart' : 'No indicator selected'}
        </div>
      </div>
    );
  }

  const headerTitle = indicator?.name || 'Indicator';
  const testId = ownPanel ? 'indicator-chart-split' : 'indicator-chart-overlay';
  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.title}>{headerTitle}</span>
      </div>
      <div className={styles.chartWrap} data-testid={testId}>
        <Chart
          traces={traces}
          layoutOverrides={layoutOverrides}
          className={styles.chart}
          downloadFilename={`indicator-${indicator?.name || 'result'}`}
        />
      </div>
    </div>
  );
}

export default IndicatorChart;
