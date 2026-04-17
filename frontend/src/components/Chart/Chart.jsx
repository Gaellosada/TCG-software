import { useCallback, useMemo, useRef } from 'react';
import Plot from 'react-plotly.js';
import useTheme from '../../hooks/useTheme';
import ErrorBoundary from '../ErrorBoundary';
import Icon from '../Icon';
import { buildBaseLayout, CHART_CONFIG } from '../../utils/chartTheme';
import { buildCsv, downloadCsv } from '../../utils/chartCsv';
import styles from './Chart.module.css';

/**
 * Shared Plotly chart wrapper with theme integration.
 *
 * All chart rendering goes through this component so every page gets
 * consistent theming, config (mode-bar, responsiveness), and sizing.
 * Wrapped in an ErrorBoundary so Plotly render errors don't crash the page.
 *
 * A CSV download button (top-right, visible on hover) exports the currently
 * visible traces — respects user visibility toggles from the Plotly legend.
 */
export default function Chart({
  traces,
  layoutOverrides,
  className,
  style,
  downloadFilename = 'chart',
}) {
  const theme = useTheme();
  const graphDivRef = useRef(null);

  const layout = useMemo(
    () => buildBaseLayout(layoutOverrides || {}, theme),
    [layoutOverrides, theme],
  );

  const handlePlotRef = useCallback((_fig, gd) => {
    graphDivRef.current = gd;
  }, []);

  const handleDownload = useCallback(() => {
    const live = graphDivRef.current?.data;
    const csv = buildCsv(live || traces);
    if (!csv) return;
    downloadCsv(csv, downloadFilename);
  }, [traces, downloadFilename]);

  const wrapperClassName = [styles.wrapper, className].filter(Boolean).join(' ');

  return (
    <div className={wrapperClassName} style={style}>
      <button
        type="button"
        className={styles.downloadBtn}
        onClick={handleDownload}
        title="Download visible series as CSV"
        aria-label="Download visible series as CSV"
      >
        <Icon name="download" size={12} />
        CSV
      </button>
      <ErrorBoundary>
        <Plot
          data={traces}
          layout={layout}
          config={CHART_CONFIG}
          useResizeHandler
          style={{ width: '100%', height: '100%' }}
          onInitialized={handlePlotRef}
          onUpdate={handlePlotRef}
        />
      </ErrorBoundary>
    </div>
  );
}
