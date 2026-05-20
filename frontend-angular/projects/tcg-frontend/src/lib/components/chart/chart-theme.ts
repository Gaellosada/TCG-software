/**
 * Shared chart theme configuration. Pure TypeScript port of React's
 * `utils/chartTheme.js` — no React imports, no Angular imports. All
 * exports are referentially stable; consumers should treat returned
 * objects as immutable.
 */

export type TcgTheme = 'light' | 'dark';

export const TRACE_COLORS = [
  '#0ea5e9',
  '#f59e0b',
  '#10b981',
  '#ef4444',
  '#8b5cf6',
  '#ec4899',
  '#06b6d4',
  '#f97316',
  '#84cc16',
  '#6366f1',
] as const;

export type ChartPalette = TcgChartPalette;

export interface TcgChartPalette {
  paper_bgcolor: string;
  plot_bgcolor: string;
  gridcolor: string;
  linecolor: string;
  tickcolor: string;
  fontColor: string;
  secondaryFont: string;
  hoverBg: string;
  hoverBorder: string;
  hoverFont: string;
  selectorBg: string;
  selectorBorder: string;
  legendBg: string;
  volumeBar: string;
  modebarColor: string;
  modebarActiveColor: string;
  markerSell: string;
  markerBuy: string;
}

const DARK_PALETTE: ChartPalette = {
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
  modebarColor: '#636b80',
  modebarActiveColor: '#e4e8f0',
  markerSell: 'rgba(59, 130, 246, 0.5)',
  markerBuy: 'rgba(59, 130, 246, 0.5)',
};

const LIGHT_PALETTE: ChartPalette = {
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
  modebarColor: '#9ca3af',
  modebarActiveColor: '#1f2937',
  markerSell: 'rgba(37, 99, 235, 0.5)',
  markerBuy: 'rgba(37, 99, 235, 0.5)',
};

const AXIS_KEYS = ['xaxis', 'xaxis2', 'xaxis3', 'yaxis', 'yaxis2', 'yaxis3'] as const;
const DEEP_MERGE_KEYS = new Set(['margin', 'modebar', 'legend']);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function getChartColors(theme: TcgTheme): ChartPalette {
  return theme === 'light' ? LIGHT_PALETTE : DARK_PALETTE;
}

export function buildBaseLayout(
  overrides: Record<string, unknown> = {},
  theme: TcgTheme = 'light',
): Record<string, unknown> {
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

  const base: Record<string, unknown> = {
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
    modebar: {
      bgcolor: 'rgba(0,0,0,0)',
      color: c.modebarColor,
      activecolor: c.modebarActiveColor,
    },
    dragmode: 'zoom',
  };

  const merged: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(overrides)) {
    if ((AXIS_KEYS as readonly string[]).includes(key) && isPlainObject(value)) {
      merged[key] = { ...axisDefaults, ...(isPlainObject(base[key]) ? base[key] : {}), ...value };
    } else if (DEEP_MERGE_KEYS.has(key) && isPlainObject(value)) {
      merged[key] = { ...(isPlainObject(base[key]) ? base[key] : {}), ...value };
    } else {
      merged[key] = value;
    }
  }

  return merged;
}

export const CHART_CONFIG = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ['lasso2d', 'select2d'],
  displayModeBar: true,
} as const;

export function createVerticalLineTrace(
  dates: ReadonlyArray<string>,
  opts: { name: string; color: string; dash: string; yaxisKey: string },
): Record<string, unknown> {
  const x: Array<string | null> = [];
  const y: Array<number | null> = [];
  for (const d of dates) {
    x.push(d, d, null);
    y.push(0, 1, null);
  }
  return {
    x,
    y,
    type: 'scatter',
    mode: 'lines',
    name: opts.name,
    line: { color: opts.color, width: 1, dash: opts.dash },
    showlegend: true,
    hoverinfo: 'skip',
    yaxis: opts.yaxisKey,
  };
}

export function hiddenOverlayAxis(): Record<string, unknown> {
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
