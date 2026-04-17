import { useMemo } from 'react';
import Plot from 'react-plotly.js';
import Plotly from 'plotly.js';
import useTheme from '../../hooks/useTheme';
import ErrorBoundary from '../ErrorBoundary';
import { buildBaseLayout, CHART_CONFIG } from '../../utils/chartTheme';
import { buildCsv, downloadCsv } from '../../utils/chartCsv';

/**
 * Shared Plotly chart wrapper with theme integration.
 *
 * All chart rendering goes through this component so every page gets
 * consistent theming, config (mode-bar, responsiveness), and sizing.
 * Wrapped in an ErrorBoundary so Plotly render errors don't crash the page.
 *
 * A CSV export button is injected into the Plotly modebar (top-right, appears
 * on hover alongside the zoom/pan/save-png controls). It exports the currently
 * visible traces — legend-hidden series are excluded.
 *
 * Pass `downloadFilename` to name the exported file; defaults to `chart.csv`.
 */
export default function Chart({
  traces,
  layoutOverrides,
  className,
  style,
  downloadFilename = 'chart',
}) {
  const theme = useTheme();

  const layout = useMemo(
    () => buildBaseLayout(layoutOverrides || {}, theme),
    [layoutOverrides, theme],
  );

  const config = useMemo(
    () => ({
      ...CHART_CONFIG,
      modeBarButtonsToAdd: [
        {
          name: 'downloadCsv',
          title: 'Download visible series as CSV',
          icon: Plotly.Icons.disk,
          click: (gd) => {
            const csv = buildCsv(gd?.data || traces);
            if (!csv) return;
            downloadCsv(csv, downloadFilename);
          },
        },
      ],
    }),
    [traces, downloadFilename],
  );

  return (
    <div className={className} style={style}>
      <ErrorBoundary>
        <Plot
          data={traces}
          layout={layout}
          config={config}
          useResizeHandler
          style={{ width: '100%', height: '100%' }}
        />
      </ErrorBoundary>
    </div>
  );
}
