import { useState, useMemo } from 'react';
import useTheme from '../../hooks/useTheme';
import Chart from '../../components/Chart';
import PillToggle from '../../components/PillToggle';
import { TRACE_COLORS, getChartColors, createVerticalLineTrace, hiddenOverlayAxis } from '../../utils/chartTheme';
import { normalizeTo100, toLongEquivalent } from '../../utils/portfolioMath';
import styles from './PortfolioEquityChart.module.css';

const DISPLAY_MODES = [
  { value: 'portfolio', label: 'Portfolio only' },
  { value: 'normalized', label: 'Normalized ($100)' },
  { value: 'weighted', label: 'Weighted' },
];

const DISPLAY_TOOLTIPS = {
  portfolio: 'Show only the combined portfolio equity curve',
  normalized: 'Each holding starts at $100 for fair comparison (ignores weight direction)',
  weighted: 'Holdings shown at their actual weighted contribution (reflects short positions)',
};

export default function PortfolioEquityChart({
  dates,
  portfolioEquity,
  legEquities,
  rawLegEquities,
  rebalanceDates,
  legs,
}) {
  const theme = useTheme();
  const [displayMode, setDisplayMode] = useState('portfolio');

  // Build a set of short-leg labels for normalized mode
  const shortLabels = useMemo(() => {
    const s = new Set();
    if (legs) {
      for (const leg of legs) {
        if (Number(leg.weight || 0) < 0) s.add(leg.label);
      }
    }
    return s;
  }, [legs]);

  const { traces, layoutOverrides } = useMemo(() => {
    if (!dates || dates.length === 0 || !portfolioEquity) {
      return { traces: [], layoutOverrides: {} };
    }

    const colors = getChartColors(theme);
    const t = [];

    // Portfolio equity — always normalized to 100
    t.push({
      x: dates,
      y: normalizeTo100(portfolioEquity),
      type: 'scatter',
      mode: 'lines',
      name: 'Portfolio',
      line: { color: TRACE_COLORS[0], width: 1.5 },
      hovertemplate: '%{x}<br>Portfolio: %{y:.1f}<extra></extra>',
    });

    // Per-leg equity curves — only in normalized and weighted modes
    if (displayMode !== 'portfolio' && legEquities) {
      // In normalized mode, use raw (buy-and-hold) leg equities to show
      // true asset performance, independent of rebalancing.
      const legSource = displayMode === 'normalized' && rawLegEquities
        ? rawLegEquities
        : legEquities;
      Object.keys(legEquities).forEach((label, idx) => {
        const colorIdx = (1 + idx) % TRACE_COLORS.length;
        let y;
        if (displayMode === 'normalized') {
          // For short legs, un-invert first so we show the underlying asset's performance
          const raw = shortLabels.has(label)
            ? toLongEquivalent(legSource[label])
            : legSource[label];
          y = normalizeTo100(raw);
        } else {
          y = legEquities[label];
        }
        t.push({
          x: dates,
          y,
          type: 'scatter',
          mode: 'lines',
          name: label,
          line: { color: TRACE_COLORS[colorIdx], width: 1 },
          hovertemplate: `%{x}<br>${label}: %{y:.1f}<extra></extra>`,
        });
      });
    }

    // Rebalance date vertical lines
    if (rebalanceDates && rebalanceDates.length > 0) {
      t.push(createVerticalLineTrace(
        rebalanceDates,
        { name: 'Rebalance', color: 'rgba(168, 85, 247, 0.35)', dash: 'dash', yaxisKey: 'y2' },
      ));
    }

    const secondaryFont = theme === 'light' ? '#6b7280' : '#636b80';

    const overrides = {
      showlegend: true,
      yaxis: {
        title: { text: displayMode === 'weighted' ? 'Equity' : 'Value ($)', font: { size: 11, color: secondaryFont } },
      },
      yaxis2: hiddenOverlayAxis(),
      margin: { l: 70 },
    };

    return { traces: t, layoutOverrides: overrides };
  }, [dates, portfolioEquity, legEquities, rawLegEquities, rebalanceDates, displayMode, shortLabels, theme]);

  if (!dates || dates.length === 0) return null;

  return (
    <div className={styles.wrapper}>
      <div className={styles.controls}>
        <span className={styles.controlLabel}>Display</span>
        <PillToggle
          options={DISPLAY_MODES}
          value={displayMode}
          onChange={setDisplayMode}
          ariaLabel="Chart display mode"
          tooltip={DISPLAY_TOOLTIPS[displayMode]}
        />
      </div>
      <Chart
        traces={traces}
        layoutOverrides={layoutOverrides}
        className={styles.container}
        downloadFilename={`portfolio-equity-${displayMode}`}
      />
    </div>
  );
}
