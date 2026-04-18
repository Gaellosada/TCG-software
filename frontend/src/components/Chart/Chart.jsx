import { useMemo } from 'react';
import Plot from 'react-plotly.js';
import useTheme from '../../hooks/useTheme';
import ErrorBoundary from '../ErrorBoundary';
import { buildBaseLayout, CHART_CONFIG } from '../../utils/chartTheme';
import { buildCsv, downloadCsv } from '../../utils/chartCsv';

// Inlined copy of Plotly's built-in `disk` icon (from plotly.js/src/fonts/ploticon.js).
// Duplicated here so this module doesn't need `import Plotly from 'plotly.js'`, which
// pulls the full plotly source into Vite's dep optimizer and fails to resolve
// `require('buffer/')` inside plotly's image trace.
const DISK_ICON = {
  width: 857.1,
  height: 1000,
  path: 'm214-7h429v214h-429v-214z m500 0h72v500q0 8-6 21t-11 20l-157 156q-5 6-19 12t-22 5v-232q0-22-15-38t-38-16h-322q-22 0-37 16t-16 38v232h-72v-714h72v232q0 22 16 38t37 16h465q22 0 38-16t15-38v-232z m-214 518v178q0 8-5 13t-13 5h-107q-7 0-13-5t-5-13v-178q0-8 5-13t13-5h107q7 0 13 5t5 13z m357-18v-518q0-22-15-38t-38-16h-750q-23 0-38 16t-16 38v750q0 22 16 38t38 16h517q23 0 50-12t42-26l156-157q16-15 27-42t11-49z',
  transform: 'matrix(1 0 0 -1 0 850)',
};

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
          icon: DISK_ICON,
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
