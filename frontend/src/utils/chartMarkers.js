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

// Key insertion order matters: it pins the Plotly trace render order. Plotly
// draws later traces ON TOP of earlier ones, and CONTRACT §C requires the
// hollow `circle-open` (sell) to render on top of the filled `circle` (buy)
// at overlapping (x, y) so the "ring-around-dot" visual is visible. So `buy`
// MUST be declared BEFORE `sell` here, and `buildAllMarkerTraces` iterates
// `Object.keys(MARKER_STYLE)` to preserve that order. Any future kind that
// is hollow-over-filled must also be inserted AFTER the kind it should
// overlay. See guardrails.md "Trace render order (Plotly z-order)".
const MARKER_STYLE = {
  buy:  { symbol: 'circle',      name: 'Roll — open',  verb: 'Open',  lineWidth: 0,   colorKey: 'markerBuy'  },
  sell: { symbol: 'circle-open', name: 'Roll — close', verb: 'Close', lineWidth: 1.5, colorKey: 'markerSell' },
};

/**
 * Build a Plotly hovertemplate string for the given marker kind.
 *
 * Customdata layout (per point): [root, expiration, strike, type, value].
 * `<extra></extra>` suppresses Plotly's default trace-name box.
 *
 * Returns `''` for unknown kinds so callers can safely template-interpolate.
 *
 * @param {'sell'|'buy'} kind
 * @returns {string}
 */
export function buildMarkerHovertemplate(kind) {
  const style = MARKER_STYLE[kind];
  if (!style) return '';
  return `<b>${style.verb}</b><br>%{customdata[0]} %{customdata[1]} %{customdata[3]} %{customdata[2]}<br>Value: %{customdata[4]:,.4f}<extra></extra>`;
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
  // Iterate MARKER_STYLE so adding a new kind is a SINGLE map-entry edit.
  // Insertion order of MARKER_STYLE pins the resulting trace render order
  // (later traces render on top) — see the MARKER_STYLE declaration above.
  const byKind = Object.fromEntries(
    Object.keys(MARKER_STYLE).map((k) => [k, []]),
  );
  for (const m of markers) {
    if (byKind[m.kind]) byKind[m.kind].push(m);
  }
  return Object.keys(MARKER_STYLE)
    .map((kind) => buildMarkerTrace(byKind[kind], kind, theme))
    .filter(Boolean);
}
