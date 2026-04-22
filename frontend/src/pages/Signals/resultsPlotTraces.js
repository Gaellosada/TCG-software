/**
 * Pure helpers for the Results subplot view.
 *
 * These helpers translate the v4 signals compute response into Plotly
 * trace arrays. The primary public API is ``buildResultsPlot()`` which
 * returns a single ``{ traces, layoutOverrides }`` for ONE Chart using
 * Plotly's domain-based subplot system (stacked vertically, shared
 * x-axis).
 *
 * Response fields consumed (v4):
 *   - ``timestamps: number[]`` (unix ms)
 *   - ``positions: [{input_id, instrument, values, clipped_mask, price}]``
 *   - ``realized_pnl: number[][]`` — one array per input (order matches
 *     ``positions``).
 *   - ``events: [{input_id, block_id, kind, fired_indices,
 *     latched_indices, active_indices, target_entry_block_id}]`` —
 *     ``kind`` is ``"entry"`` or ``"exit"``. Color/direction comes from
 *     the sign of the originating block's signed weight (for entries) or
 *     the targeted entry's weight (for exits). That mapping lives in the
 *     caller — we receive it via ``blockWeightSigns``.
 *   - ``indicators: [{input_id, indicator_id, series}]`` — indicator
 *     traces to overlay on the bottom plot.
 *
 * Marker convention (v4):
 *   entry + positive weight → green ▲  filled (long entry)
 *   entry + negative weight → red   ▼  filled (short entry)
 *   entry + zero weight     → grey ■  filled (defensive — backend rejects weight=0)
 *   exit  + positive target → green ▼  open (closes a long)
 *   exit  + negative target → red   ▲  open (closes a short)
 *   exit  + zero/unknown    → grey ▼  open (defensive)
 */
import { TRACE_COLORS } from '../../utils/chartTheme';

const COLOR_LONG = '#10b981';
const COLOR_SHORT = '#ef4444';
const COLOR_NEUTRAL = '#9ca3af';

/**
 * Marker style per ``(kind, weightSign)``. Exported for test assertions.
 *
 * ``weightSign`` is ``1`` (positive weight), ``-1`` (negative weight),
 * or ``0`` (zero / unknown — defensive fallback only; backend rejects
 * zero-weight entries and exits with dangling targets).
 */
export const EVENT_MARKER = {
  entry: {
    1: { symbol: 'triangle-up',   color: COLOR_LONG,    open: false, label: 'long entry'  },
    '-1': { symbol: 'triangle-down', color: COLOR_SHORT, open: false, label: 'short entry' },
    0: { symbol: 'square',        color: COLOR_NEUTRAL, open: false, label: 'entry'       },
  },
  exit: {
    1: { symbol: 'triangle-down', color: COLOR_LONG,    open: true, label: 'long exit'   },
    '-1': { symbol: 'triangle-up',   color: COLOR_SHORT, open: true, label: 'short exit'  },
    0: { symbol: 'triangle-down', color: COLOR_NEUTRAL, open: true, label: 'exit'        },
  },
};

/**
 * Resolve the marker style for an event, given a sign-lookup map.
 *
 * ``blockWeightSigns`` keys every known block id (entries AND exits) to
 * the *effective* weight sign for colouring purposes:
 *   - entry block → sign(weight) of that entry
 *   - exit block  → sign(weight) of the entry it targets
 *
 * Returns ``null`` if the event's kind is unknown.
 */
function resolveMarker(ev, blockWeightSigns) {
  const kindTable = EVENT_MARKER[ev.kind];
  if (!kindTable) return null;
  const signRaw = blockWeightSigns ? blockWeightSigns[ev.block_id] : undefined;
  // Default to 0 (neutral) when the caller doesn't provide a map or the
  // block id is missing. This keeps the function robust for test
  // fixtures that only care about mode='markers'.
  const sign = (signRaw === 1 || signRaw === -1) ? signRaw : 0;
  return kindTable[sign] || kindTable[0];
}

/**
 * Build a block-id → weight-sign map from a v4 signal's rules.
 *
 * For entry blocks the sign is ``sign(weight)``; for exit blocks the
 * sign is ``sign(weight)`` of the *targeted* entry. Blocks whose target
 * is missing or whose weight is zero map to ``0`` (neutral) — a
 * defensive value the marker table falls back on.
 */
export function buildBlockWeightSignMap(rules) {
  const map = {};
  if (!rules || typeof rules !== 'object') return map;
  const entries = Array.isArray(rules.entries) ? rules.entries : [];
  const exits = Array.isArray(rules.exits) ? rules.exits : [];
  const entrySignById = {};
  for (const e of entries) {
    if (!e || !e.id) continue;
    const w = typeof e.weight === 'number' ? e.weight : 0;
    const s = w > 0 ? 1 : (w < 0 ? -1 : 0);
    entrySignById[e.id] = s;
    map[e.id] = s;
  }
  for (const x of exits) {
    if (!x || !x.id) continue;
    const tgt = x.target_entry_block_id;
    const s = (tgt && Object.prototype.hasOwnProperty.call(entrySignById, tgt))
      ? entrySignById[tgt]
      : 0;
    map[x.id] = s;
  }
  return map;
}

function toDates(timestamps) {
  return timestamps.map((ms) => new Date(ms));
}

/**
 * Build the per-input price traces shared by both plots. Skips inputs
 * without a price payload rather than synthesizing dummy data.
 *
 * @param {Array} positions   v4 ``positions`` array
 * @param {Date[]} dates      pre-built Date array, length == timestamps.length
 * @param {object} [opts]     options
 * @param {string} [opts.yaxis]  Plotly yaxis ref (e.g. 'y2') for subplot targeting
 * @param {string} [opts.legendGroupSuffix]  suffix appended to legendgroup
 * @returns Plotly trace objects, one per input that has a price.
 */
export function buildInputTraces(positions, dates, opts = {}) {
  const traces = [];
  positions.forEach((p, idx) => {
    if (!p || !p.price || !Array.isArray(p.price.values)) return;
    const color = TRACE_COLORS[idx % TRACE_COLORS.length];
    const trace = {
      x: dates,
      y: p.price.values,
      type: 'scatter',
      mode: 'lines',
      name: `${p.input_id} — ${p.price.label || 'price'}`,
      line: { color, width: 1.5 },
      connectgaps: false,
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      legendgroup: `input-${p.input_id}${opts.legendGroupSuffix || ''}`,
    };
    if (opts.yaxis) trace.yaxis = opts.yaxis;
    traces.push(trace);
  });
  return traces;
}

/**
 * Sum ``realized_pnl`` across inputs into a single aggregate series.
 * Decision (logged to output): aggregate = summed-across-inputs line.
 * Rationale: the ask says "realized P&L line" (singular); summing across
 * inputs gives the portfolio-level curve which is what a user reading
 * "P&L" expects to see.
 *
 * @param {number[][] | undefined} realizedPnl
 * @param {number} len   timestamps.length — guards against ragged arrays
 * @returns number[] | null  null if the payload is absent/empty
 */
export function aggregateRealizedPnl(realizedPnl, len) {
  if (!Array.isArray(realizedPnl) || realizedPnl.length === 0) return null;
  const out = new Array(len).fill(0);
  let anyFinite = false;
  for (const series of realizedPnl) {
    if (!Array.isArray(series)) continue;
    for (let i = 0; i < len; i++) {
      const v = series[i];
      if (typeof v === 'number' && Number.isFinite(v)) {
        out[i] += v;
        anyFinite = true;
      }
    }
  }
  return anyFinite ? out : null;
}

/**
 * Convert an events entry into a scatter-marker trace.
 *
 * y values are pinned to the associated input's price at that bar, so
 * markers sit on the price line. If the input has no price, the
 * marker trace is skipped (don't synthesize).
 *
 * Bars are taken from ``fired_indices``. The "don't repeat" filter is
 * applied upstream by ``computeEffectiveTrace`` (runGate.js), which
 * rewrites ``fired_indices`` to the backend-authoritative
 * ``latched_indices`` when the flag is on. This function is therefore
 * kind-agnostic: it trusts the event payload.
 *
 * @param {Array}  events
 * @param {Array}  positions
 * @param {Date[]} dates
 * @param {string} [yaxis]  Plotly yaxis ref for subplot targeting
 * @param {object} [opts]
 * @param {Object<string, number>} [opts.blockWeightSigns]  block-id → sign
 *        map used to pick the marker style per (kind, sign). Omit or
 *        pass ``{}`` and every event falls back to the neutral style.
 */
export function buildEventMarkerTraces(
  events,
  positions,
  dates,
  yaxis,
  { blockWeightSigns } = {},
) {
  if (!Array.isArray(events) || events.length === 0) return [];
  const byInput = new Map();
  for (const p of positions) {
    if (p && p.input_id) byInput.set(p.input_id, p);
  }
  const traces = [];
  for (const ev of events) {
    if (!ev || !ev.kind) continue;
    const style = resolveMarker(ev, blockWeightSigns);
    if (!style) continue; // unknown kind — skip gracefully
    const position = byInput.get(ev.input_id);
    if (!position || !position.price || !Array.isArray(position.price.values)) continue;
    const indices = Array.isArray(ev.fired_indices) ? ev.fired_indices : [];
    if (indices.length === 0) continue;
    const xs = [];
    const ys = [];
    for (const i of indices) {
      if (i < 0 || i >= dates.length) continue;
      const priceAtBar = position.price.values[i];
      if (priceAtBar === null || priceAtBar === undefined) continue;
      xs.push(dates[i]);
      ys.push(priceAtBar);
    }
    if (xs.length === 0) continue;
    const blockLabel = ev.block_id ? ` (block ${ev.block_id})` : '';
    const trace = {
      x: xs,
      y: ys,
      type: 'scatter',
      mode: 'markers',
      name: `${style.label} • ${ev.input_id}${blockLabel}`,
      marker: {
        symbol: style.open ? `${style.symbol}-open` : style.symbol,
        color: style.color,
        size: 11,
        line: { color: style.color, width: 1.5 },
      },
      hovertemplate: `%{x}<br>${style.label} on ${ev.input_id}<extra></extra>`,
      legendgroup: `events-${ev.block_id || ev.kind}`,
      showlegend: true,
    };
    if (yaxis) trace.yaxis = yaxis;
    traces.push(trace);
  }
  return traces;
}

/**
 * Build indicator traces for a plot. One line per indicator entry.
 * Colours cycle through TRACE_COLORS starting from the index AFTER the
 * input traces to avoid colour collisions.
 *
 * @param {string} [yaxis]  Plotly yaxis ref (e.g. 'y2'). Defaults to 'y2'
 *                           for backward compat with the old bottom-plot
 *                           callers.
 */
export function buildIndicatorTraces(indicators, dates, colorOffset = 0, yaxis = 'y2') {
  if (!Array.isArray(indicators) || indicators.length === 0) return [];
  return indicators
    .filter((ind) => ind && Array.isArray(ind.series))
    .map((ind, i) => ({
      x: dates,
      y: ind.series,
      type: 'scatter',
      mode: 'lines',
      name: `ind: ${ind.indicator_id}${ind.input_id ? ` • ${ind.input_id}` : ''}`,
      line: {
        color: TRACE_COLORS[(colorOffset + i) % TRACE_COLORS.length],
        width: 1,
        dash: 'dot',
      },
      yaxis,
      connectgaps: false,
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      legendgroup: `ind-${ind.indicator_id}`,
    }));
}

/**
 * Partition indicators into overlay (rendered on the bottom plot) and
 * ownPanel (each gets a dedicated Chart instance below the bottom plot).
 */
export function partitionIndicators(indicators) {
  const overlay = [];
  const ownPanel = [];
  if (!Array.isArray(indicators)) return { overlay, ownPanel };
  for (const ind of indicators) {
    if (ind.ownPanel) ownPanel.push(ind);
    else overlay.push(ind);
  }
  return { overlay, ownPanel };
}

/* ------------------------------------------------------------------ */
/*  Unified subplot builder — single Chart, stacked vertically         */
/* ------------------------------------------------------------------ */

/** Shared y-axis styling applied to every subplot axis. */
const SUBPLOT_YAXIS_BASE = {
  showgrid: true,
  gridcolor: 'rgba(150,150,150,0.15)',
  zeroline: false,
};

/** Gap between vertically stacked subplots (fraction of total height). */
const SUBPLOT_GAP = 0.03;

/**
 * Compute domain arrays (vertical fraction) for each subplot.
 *
 * - Top and Bottom subplots share ~80% of the chart height equally.
 * - Each ownPanel subplot shares the remaining ~20% equally.
 * - A small gap separates each subplot.
 *
 * Returns an array of ``[lower, upper]`` domain pairs, ordered:
 * ``[top, bottom, ownPanel0, ownPanel1, ...]`` where index 0 is the
 * highest on screen (Plotly domain 0 = bottom, 1 = top, so higher
 * domains appear higher).
 */
export function computeSubplotDomains(ownPanelCount) {
  const n = 2 + ownPanelCount; // total subplots
  const totalGap = SUBPLOT_GAP * (n - 1);
  const usable = 1 - totalGap;

  // Fraction allocated to the two main subplots vs ownPanel subplots.
  const mainFraction = ownPanelCount > 0 ? 0.8 : 1.0;
  const panelFraction = 1.0 - mainFraction;

  const mainHeight = (usable * mainFraction) / 2;
  const panelHeight = ownPanelCount > 0
    ? (usable * panelFraction) / ownPanelCount
    : 0;

  // Build domains top-down (highest first).
  const domains = [];
  let cursor = 1; // start from the top

  // Top subplot
  domains.push([cursor - mainHeight, cursor]);
  cursor -= mainHeight + SUBPLOT_GAP;

  // Bottom subplot
  domains.push([cursor - mainHeight, cursor]);
  cursor -= mainHeight + SUBPLOT_GAP;

  // ownPanel subplots
  for (let i = 0; i < ownPanelCount; i++) {
    domains.push([cursor - panelHeight, cursor]);
    cursor -= panelHeight + SUBPLOT_GAP;
  }

  // Clamp to [0,1] for floating-point safety.
  return domains.map(([lo, hi]) => [
    Math.max(0, Math.round(lo * 1000) / 1000),
    Math.min(1, Math.round(hi * 1000) / 1000),
  ]);
}

/**
 * Primary public API — builds a single ``{ traces, layoutOverrides, hasData }``
 * for one unified Chart with domain-based Plotly subplots.
 *
 * Subplot layout:
 *   - Subplot 1 (top):    price inputs + aggregated P&L (same y-axis)
 *   - Subplot 2 (bottom): price inputs + overlay indicators + event markers
 *   - Subplots 3..N:      one per ownPanel indicator (indicator line only)
 *
 * All subplots share a single x-axis for linked zoom/pan.
 * No right-axis (yaxis with side:'right') is used anywhere.
 *
 * @param {object} result  compute-signal response payload
 * @param {object} [opts]
 * @param {number}  [opts.capital=1]   equity-curve scaling
 * @param {object}  [opts.signalRules] v4 ``rules`` ({entries, exits}) used to
 *        resolve per-block weight signs for marker colouring. If omitted
 *        every marker falls back to the neutral style.
 */
export function buildResultsPlot(result, opts = {}) {
  if (!result || !Array.isArray(result.timestamps) || result.timestamps.length === 0) {
    return { traces: [], layoutOverrides: {}, hasData: false };
  }

  const positions = Array.isArray(result.positions) ? result.positions : [];
  const dates = toDates(result.timestamps);
  const { overlay, ownPanel } = partitionIndicators(
    Array.isArray(result.indicators) ? result.indicators : [],
  );
  const ownPanelWithData = ownPanel.filter(
    (ind) => Array.isArray(ind.series) && ind.series.length > 0,
  );

  const domains = computeSubplotDomains(ownPanelWithData.length);
  const traces = [];

  // --- Subplot 1 (top): prices + P&L + capital, uses yaxis 'y' ---
  const topInputTraces = buildInputTraces(positions, dates, { legendGroupSuffix: '-top' });
  traces.push(...topInputTraces);

  const pnlRaw = aggregateRealizedPnl(result.realized_pnl, result.timestamps.length);
  if (pnlRaw) {
    const capital = opts.capital ?? 1;
    const pnlScaled = capital === 1 ? pnlRaw : pnlRaw.map((v) => v * capital);
    traces.push({
      x: dates,
      y: pnlScaled,
      type: 'scatter',
      mode: 'lines',
      name: 'realized P&L',
      line: { color: '#a78bfa', width: 2, dash: 'solid' },
      hovertemplate: '%{x}<br>P&L %{y:,.2f}<extra></extra>',
      legendgroup: 'pnl',
    });
    // Equity curve: initial capital + cumulative P&L
    const equity = pnlScaled.map((v) => capital + v);
    traces.push({
      x: dates,
      y: equity,
      type: 'scatter',
      mode: 'lines',
      name: 'capital',
      line: { color: '#22d3ee', width: 2, dash: 'solid' },
      hovertemplate: '%{x}<br>Capital %{y:,.2f}<extra></extra>',
      legendgroup: 'capital',
    });
  }

  // --- Subplot 2 (bottom): prices + overlay indicators + events, uses yaxis 'y2' ---
  const bottomInputTraces = buildInputTraces(positions, dates, {
    yaxis: 'y2',
  });
  traces.push(...bottomInputTraces);

  // Overlay indicators share y2 with price (no separate right axis).
  // Offset colours past the input traces to avoid collisions.
  const overlayTraces = buildIndicatorTraces(overlay, dates, bottomInputTraces.length, 'y2');
  traces.push(...overlayTraces);

  const events = Array.isArray(result.events) ? result.events : [];
  const blockWeightSigns = opts.signalRules
    ? buildBlockWeightSignMap(opts.signalRules)
    : undefined;
  const eventTraces = buildEventMarkerTraces(events, positions, dates, 'y2', {
    blockWeightSigns,
  });
  traces.push(...eventTraces);

  // --- Subplots 3..N: one per ownPanel indicator ---
  // Offset colours past the price + overlay traces to avoid shared colours.
  const ownPanelColorOffset = bottomInputTraces.length + overlayTraces.length;
  ownPanelWithData.forEach((ind, i) => {
    const axisRef = `y${i + 3}`;
    traces.push({
      x: dates,
      y: ind.series,
      type: 'scatter',
      mode: 'lines',
      name: `ind: ${ind.indicator_id}${ind.input_id ? ` • ${ind.input_id}` : ''}`,
      line: { color: TRACE_COLORS[(ownPanelColorOffset + i) % TRACE_COLORS.length], width: 1.5 },
      yaxis: axisRef,
      connectgaps: false,
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
    });
  });

  // --- Layout ---
  const lo = {
    showlegend: true,
    legend: { orientation: 'h', y: -0.08 },
    // Single shared x-axis anchored to the lowest subplot.
    xaxis: {
      title: { text: '' },
      showgrid: true,
      gridcolor: 'rgba(150,150,150,0.15)',
      // Anchor to the bottom-most yaxis so ticks appear at the bottom.
      anchor: ownPanelWithData.length > 0
        ? `y${ownPanelWithData.length + 2}`
        : 'y2',
    },
    // Top subplot y-axis
    yaxis: {
      ...SUBPLOT_YAXIS_BASE,
      domain: domains[0],
      title: { text: 'prices + P&L + capital' },
      anchor: 'x',
    },
    // Bottom subplot y-axis
    yaxis2: {
      ...SUBPLOT_YAXIS_BASE,
      domain: domains[1],
      title: { text: 'prices + indicators' },
      anchor: 'x',
    },
  };

  // ownPanel y-axes
  ownPanelWithData.forEach((ind, i) => {
    const key = `yaxis${i + 3}`;
    lo[key] = {
      ...SUBPLOT_YAXIS_BASE,
      domain: domains[i + 2],
      title: { text: ind.indicator_id },
      anchor: 'x',
    };
  });

  // Horizontal separator lines between subplots.
  // Place a thin line at the bottom of each domain (except the last).
  const separators = [];
  for (let i = 0; i < domains.length - 1; i++) {
    const y = domains[i][0] - SUBPLOT_GAP / 2;
    separators.push({
      type: 'line',
      xref: 'paper',
      yref: 'paper',
      x0: 0,
      x1: 1,
      y0: y,
      y1: y,
      line: { color: 'rgba(150,150,150,0.3)', width: 1 },
    });
  }
  lo.shapes = separators;

  return {
    traces,
    layoutOverrides: lo,
    hasData: traces.length > 0,
  };
}

/**
 * Count the number of ownPanel indicators that have data, used by
 * ResultsView to calculate the container height.
 */
export function countOwnPanelIndicators(result) {
  if (!result || !Array.isArray(result.indicators)) return 0;
  return result.indicators.filter(
    (ind) => ind.ownPanel && Array.isArray(ind.series) && ind.series.length > 0,
  ).length;
}
