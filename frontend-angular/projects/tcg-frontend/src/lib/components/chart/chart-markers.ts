/**
 * Shared marker-trace builder for the Chart component. Pure TS port of
 * React `utils/chartMarkers.js`. The MARKER_STYLE map's insertion order
 * pins the Plotly trace render order (later traces draw on top); `buy`
 * MUST be declared BEFORE `sell` so the hollow circle-open renders on
 * top of the filled circle at overlapping (x, y).
 */

import { TcgTheme, getChartColors } from './chart-theme';

export interface TcgContractMeta {
  contract_id?: string;
  root?: string;
  expiration?: string;
  strike?: number;
  type?: 'C' | 'P';
  value?: number | null;
}

export interface TcgChartMarker {
  x: string | number;
  y: number;
  kind: 'sell' | 'buy';
  tooltip?: TcgContractMeta;
  customdata?: unknown[];
}

// Back-compat aliases — prefer the Tcg-prefixed names.
export type ContractMeta = TcgContractMeta;
export type ChartMarker = TcgChartMarker;

interface MarkerStyle {
  symbol: string;
  name: string;
  verb: 'Sell' | 'Buy';
  lineWidth: number;
  colorKey: 'markerSell' | 'markerBuy';
}

const MARKER_STYLE: Record<'buy' | 'sell', MarkerStyle> = {
  buy: { symbol: 'circle', name: 'Roll — buy', verb: 'Buy', lineWidth: 0, colorKey: 'markerBuy' },
  sell: {
    symbol: 'circle-open',
    name: 'Roll — sell',
    verb: 'Sell',
    lineWidth: 1.5,
    colorKey: 'markerSell',
  },
};

export function buildMarkerHovertemplate(kind: 'sell' | 'buy'): string {
  const style = MARKER_STYLE[kind];
  if (!style) return '';
  return `<b>${style.verb}</b><br>%{customdata[0]} %{customdata[1]} %{customdata[3]} %{customdata[2]}<br>Value: %{customdata[4]:,.4f}<extra></extra>`;
}

export function buildMarkerTrace(
  markersOfKind: ReadonlyArray<TcgChartMarker>,
  kind: 'sell' | 'buy',
  theme: TcgTheme,
  hovertemplate?: string,
): Record<string, unknown> | null {
  if (!markersOfKind || markersOfKind.length === 0) return null;
  const style = MARKER_STYLE[kind];
  if (!style) return null;
  const colors = getChartColors(theme);
  const color = colors[style.colorKey];
  return {
    x: markersOfKind.map((m) => m.x),
    y: markersOfKind.map((m) => m.y),
    customdata: markersOfKind.map((m) => {
      if (Array.isArray(m.customdata)) return m.customdata;
      const tip = m.tooltip || {};
      return [tip.root, tip.expiration, tip.strike, tip.type, tip.value];
    }),
    type: 'scatter',
    mode: 'markers',
    name: style.name,
    marker: {
      symbol: style.symbol,
      size: 8,
      color,
      line: { width: style.lineWidth, color },
    },
    hovertemplate: hovertemplate ?? buildMarkerHovertemplate(kind),
    legendgroup: 'roll-markers',
    showlegend: true,
    meta: { skipCsv: true },
  };
}

export interface TcgBuildAllMarkerTracesOpts {
  hovertemplates?: Partial<Record<'sell' | 'buy', string>>;
}
export type BuildAllMarkerTracesOpts = TcgBuildAllMarkerTracesOpts;

export function buildAllMarkerTraces(
  markers: ReadonlyArray<TcgChartMarker> | null | undefined,
  theme: TcgTheme,
  opts: TcgBuildAllMarkerTracesOpts = {},
): Array<Record<string, unknown>> {
  if (!markers || markers.length === 0) return [];
  const hovertemplates = opts.hovertemplates || {};
  const byKind: Record<'buy' | 'sell', TcgChartMarker[]> = { buy: [], sell: [] };
  for (const m of markers) {
    if (byKind[m.kind]) byKind[m.kind].push(m);
  }
  const kinds: Array<'buy' | 'sell'> = ['buy', 'sell'];
  return kinds
    .map((kind) => buildMarkerTrace(byKind[kind], kind, theme, hovertemplates[kind]))
    .filter((t): t is Record<string, unknown> => t !== null);
}
