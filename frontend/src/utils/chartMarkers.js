/**
 * Shared marker-trace builder for the Chart component.
 *
 * Sole producer of Plotly marker traces for kind-discriminated overlays
 * (currently option-roll sell/buy circles; future kinds will plug in
 * through the `MARKER_STYLE` map below — single source of truth).
 *
 * Design constraints (CONTRACT B.4):
 *   - ONE helper used for BOTH sell and buy. No duplicated render logic.
 *   - Empty-vs-filled distinction lives declaratively in `MARKER_STYLE`,
 *     NOT in inline if/else branches at the render path.
 *   - Theme colors come from the palette only (chartTheme.js). No
 *     hardcoded hex anywhere in this module.
 *
 * @typedef {Object} ContractMeta
 * @property {string} [contract_id]
 * @property {string} root
 * @property {string} expiration   ISO "YYYY-MM-DD"
 * @property {number} strike
 * @property {'C'|'P'} type
 * @property {number|null} value
 *
 * @typedef {Object} Marker
 * @property {string|number} x
 * @property {number} y
 * @property {'sell'|'buy'} kind
 * @property {ContractMeta} tooltip
 */

import { getChartColors } from './chartTheme';

const MARKER_STYLE = {
  sell: { symbol: 'circle-open', name: 'Roll — close', lineWidth: 1.5, colorKey: 'markerSell' },
  buy:  { symbol: 'circle',      name: 'Roll — open',  lineWidth: 0,   colorKey: 'markerBuy'  },
};

/**
 * Build a Plotly hovertemplate string for the given marker kind.
 *
 * Customdata layout (per point): [root, expiration, strike, type, value].
 * `<extra></extra>` suppresses Plotly's default trace-name box.
 *
 * @param {'sell'|'buy'} kind
 * @returns {string}
 */
export function buildMarkerHovertemplate(kind) {
  const verb = kind === 'sell' ? 'Close' : 'Open';
  return `<b>${verb}</b><br>%{customdata[0]} %{customdata[1]} %{customdata[3]} %{customdata[2]}<br>Value: %{customdata[4]:,.4f}<extra></extra>`;
}

/**
 * Build one Plotly scatter trace for a single marker kind.
 *
 * Returns `null` when there is nothing to draw (empty input or unknown
 * kind) so the caller can `.filter(Boolean)` without thinking about it.
 *
 * @param {Marker[]} markersOfKind
 * @param {'sell'|'buy'} kind
 * @param {'dark'|'light'} theme
 * @returns {object|null}
 */
export function buildMarkerTrace(markersOfKind, kind, theme) {
  if (!markersOfKind || markersOfKind.length === 0) return null;
  const style = MARKER_STYLE[kind];
  if (!style) return null;
  const colors = getChartColors(theme);
  const color = colors[style.colorKey];
  return {
    x: markersOfKind.map((m) => m.x),
    y: markersOfKind.map((m) => m.y),
    customdata: markersOfKind.map((m) => [
      m.tooltip.root,
      m.tooltip.expiration,
      m.tooltip.strike,
      m.tooltip.type,
      m.tooltip.value,
    ]),
    type: 'scatter',
    mode: 'markers',
    name: style.name,
    marker: {
      symbol: style.symbol,
      size: 8,
      color,
      line: { width: style.lineWidth, color },
    },
    hovertemplate: buildMarkerHovertemplate(kind),
    legendgroup: 'roll-markers',
    showlegend: true,
  };
}

/**
 * Build the full set of marker traces for a flat `markers` array.
 *
 * Markers are grouped by kind; one Plotly scatter trace is emitted per
 * non-empty kind. Returns `[]` when there is nothing to draw.
 *
 * @param {Marker[]|undefined|null} markers
 * @param {'dark'|'light'} theme
 * @returns {object[]}
 */
export function buildAllMarkerTraces(markers, theme) {
  if (!markers || markers.length === 0) return [];
  const byKind = { sell: [], buy: [] };
  for (const m of markers) {
    if (byKind[m.kind]) byKind[m.kind].push(m);
  }
  return [
    buildMarkerTrace(byKind.sell, 'sell', theme),
    buildMarkerTrace(byKind.buy, 'buy', theme),
  ].filter(Boolean);
}
