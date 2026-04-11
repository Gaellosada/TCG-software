import { useMemo } from 'react';
import Plot from 'react-plotly.js';
import useTheme from '../../hooks/useTheme';
import ErrorBoundary from '../ErrorBoundary';
import { buildBaseLayout, CHART_CONFIG } from '../../utils/chartTheme';

/**
 * Shared Plotly chart wrapper with theme integration.
 *
 * All chart rendering goes through this component so every page gets
 * consistent theming, config (mode-bar, responsiveness), and sizing.
 * Wrapped in an ErrorBoundary so Plotly render errors don't crash the page.
 */
export default function Chart({ traces, layoutOverrides, className, style }) {
  const theme = useTheme();

  const layout = useMemo(
    () => buildBaseLayout(layoutOverrides || {}, theme),
    [layoutOverrides, theme],
  );

  return (
    <div className={className} style={style}>
      <ErrorBoundary>
        <Plot
          data={traces}
          layout={layout}
          config={CHART_CONFIG}
          useResizeHandler
          style={{ width: '100%', height: '100%' }}
        />
      </ErrorBoundary>
    </div>
  );
}
