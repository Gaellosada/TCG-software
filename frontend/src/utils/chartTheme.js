/**
 * Shared chart theme configuration for all Plotly charts.
 * Individual charts import buildBaseLayout() and pass overrides.
 */

export const TRACE_COLORS = [
  '#0ea5e9', '#f59e0b', '#10b981', '#ef4444',
  '#8b5cf6', '#ec4899', '#06b6d4', '#f97316',
  '#84cc16', '#6366f1',
];

const DARK_PALETTE = {
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)',
  gridcolor: '#1e2130',
  linecolor: '#2a2e3e',
  tickcolor: '#2a2e3e',
  fontColor: '#9ba3b8',
  secondaryFont: '#636b80',
  hoverBg: 'rgba(0,0,0,0.4)',
  hoverBorder: 'rgba(255,255,255,0.1)',
  hoverFont: '#e4e8f0',
  selectorBg: 'rgba(22,25,34,0.9)',
  selectorBorder: 'rgba(42,46,62,0.8)',
  legendBg: 'rgba(13,15,24,0.7)',
  volumeBar: 'rgba(14, 165, 233, 0.3)',
};

const LIGHT_PALETTE = {
  paper_bgcolor: 'rgba(255,255,255,0)',
  plot_bgcolor: 'rgba(255,255,255,0)',
  gridcolor: '#e5e7eb',
  linecolor: '#d1d5db',
  tickcolor: '#d1d5db',
  fontColor: '#374151',
  secondaryFont: '#6b7280',
  hoverBg: 'rgba(255,255,255,0.85)',
  hoverBorder: 'rgba(0,0,0,0.1)',
  hoverFont: '#1a1a1a',
  selectorBg: 'rgba(240,240,240,0.9)',
  selectorBorder: 'rgba(209,213,219,0.8)',
  legendBg: 'rgba(255,255,255,0.7)',
  volumeBar: 'rgba(2, 132, 199, 0.25)',
};

const AXIS_KEYS = ['xaxis', 'xaxis2', 'xaxis3', 'yaxis', 'yaxis2', 'yaxis3'];

/**
 * Returns the palette object for the given theme.
 */
export function getChartColors(theme) {
  return theme === 'light' ? LIGHT_PALETTE : DARK_PALETTE;
}

/**
 * Builds a Plotly layout from base defaults + overrides.
 * Axis keys are deep-merged; non-axis keys are overwritten.
 */
export function buildBaseLayout(overrides = {}, theme = 'light') {
  const c = getChartColors(theme);

  const axisDefaults = {
    gridcolor: c.gridcolor,
    linecolor: c.linecolor,
    tickcolor: c.tickcolor,
    zeroline: false,
  };

  const spikeDefaults = {
    showspikes: true,
    spikemode: 'across',
    spikethickness: 1,
    spikecolor: 'rgba(155,163,184,0.4)',
    spikedash: 'dot',
  };

  const base = {
    autosize: true,
    paper_bgcolor: c.paper_bgcolor,
    plot_bgcolor: c.plot_bgcolor,
    font: {
      family: 'Outfit, system-ui, sans-serif',
      size: 12,
      color: c.fontColor,
    },
    xaxis: { ...axisDefaults, ...spikeDefaults, type: 'date', rangeslider: { visible: false } },
    yaxis: { ...axisDefaults, ...spikeDefaults },
    legend: {
      orientation: 'h',
      yanchor: 'top',
      y: -0.12,
      xanchor: 'center',
      x: 0.5,
      font: { size: 11 },
      bgcolor: 'rgba(0,0,0,0)',
    },
    hovermode: 'x unified',
    spikedistance: -1,
    hoverlabel: {
      bgcolor: c.hoverBg,
      bordercolor: c.hoverBorder,
      font: { color: c.hoverFont, size: 11 },
    },
    margin: { l: 60, r: 24, t: 40, b: 60 },
    dragmode: 'zoom',
  };

  // Deep-merge axis keys, overwrite everything else
  const merged = { ...base };
  for (const [key, value] of Object.entries(overrides)) {
    if (AXIS_KEYS.includes(key) && typeof value === 'object') {
      merged[key] = { ...axisDefaults, ...(base[key] || {}), ...value };
    } else {
      merged[key] = value;
    }
  }

  return merged;
}

/**
 * Shared Plotly config — used by all charts.
 */
export const CHART_CONFIG = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ['lasso2d', 'select2d'],
  displayModeBar: true,
};

/**
 * Build a scatter trace that draws toggleable vertical lines on a hidden overlay axis.
 * Each date gets a line from y=0 to y=1 on the overlay axis (full chart height).
 */
export function createVerticalLineTrace(dates, { name, color, dash, yaxisKey }) {
  const x = [];
  const y = [];
  for (const d of dates) {
    x.push(d, d, null);
    y.push(0, 1, null);
  }
  return {
    x,
    y,
    type: 'scatter',
    mode: 'lines',
    name,
    line: { color, width: 1, dash },
    showlegend: true,
    hoverinfo: 'skip',
    yaxis: yaxisKey,
  };
}

/**
 * Layout config for a hidden y-axis used to anchor vertical line traces.
 * Overlays the main y-axis, spans [0,1], invisible.
 */
export function hiddenOverlayAxis() {
  return {
    overlaying: 'y',
    range: [0, 1],
    fixedrange: true,
    showgrid: false,
    showticklabels: false,
    zeroline: false,
    visible: false,
  };
}
