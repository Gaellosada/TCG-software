/**
 * Pure helpers for the Results subplot view.
 *
 * These helpers translate the v3 + iter-5 signals compute response into
 * Plotly trace arrays. The primary public API is ``buildResultsPlot()``
 * which returns a single ``{ traces, layoutOverrides }`` for ONE Chart
 * using Plotly's domain-based subplot system (stacked vertically, shared
 * x-axis).
 *
 * Response fields consumed (payload contract P5-6):
 *   - ``timestamps: number[]`` (unix ms)
 *   - ``positions: [{input_id, instrument, values, clipped_mask, price}]``
 *   - ``realized_pnl: number[][]`` — one array per input (order matches
 *     ``positions``). Optional in payload while I1 work catches up;
 *     when absent we skip the P&L trace.
 *   - ``events: [{input_id, block_id, kind, fired_indices,
 *     latched_indices}]`` — entry/exit markers.
 *   - ``indicators: [{input_id, indicator_id, series}]`` — indicator
 *     traces to overlay on the bottom plot.
 *
 * Constraints:
 *   - Use TRACE_COLORS from chartTheme so colours match the rest of
 *     the app.
 *   - Don't synthesize data — if a field is missing, skip its trace.
 *   - Entry/exit marker convention (R1 brief):
 *       long_entry  → green ▲  (symbol 'triangle-up',   #10b981)
 *       long_exit   → green ▼  (symbol 'triangle-down', #10b981, open)
 *       short_entry → red   ▼  (symbol 'triangle-down', #ef4444)
 *       short_exit  → red   ▲  (symbol 'triangle-up',   #ef4444, open)
 */
import { TRACE_COLORS } from '../../utils/chartTheme';

/** Marker style per event kind. Kept exported for test assertions. */
export const EVENT_MARKER = {
  long_entry:  { symbol: 'triangle-up',   color: '#10b981', open: false, label: 'long entry'  },
  long_exit:   { symbol: 'triangle-down', color: '#10b981', open: true,  label: 'long exit'   },
  short_entry: { symbol: 'triangle-down', color: '#ef4444', open: false, label: 'short entry' },
  short_exit:  { symbol: 'triangle-up',   color: '#ef4444', open: true,  label: 'short exit'  },
};

function toDates(timestamps) {
  return timestamps.map((ms) => new Date(ms));
}

/**
 * Build the per-input price traces shared by both plots. Skips inputs
 * without a price payload rather than synthesizing dummy data.
 *
 * @param {Array} positions   v3 ``positions`` array
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
 * Convert an events entry into a scatter-marker trace. Uses
 * ``latched_indices`` preferentially (R1 convention) — these are bars
 * where a block's latching action took effect. Falls back to
 * ``fired_indices`` if ``latched_indices`` is absent.
 *
 * y values are pinned to the associated input's price at that bar, so
 * markers sit on the price line. If the input has no price, the
 * marker trace is skipped (don't synthesize).
 *
 * @param {string} [yaxis]  Plotly yaxis ref for subplot targeting
 */
export function buildEventMarkerTraces(events, positions, dates, yaxis, { noRepeat = false } = {}) {
  if (!Array.isArray(events) || events.length === 0) return [];
  const byInput = new Map();
  for (const p of positions) {
    if (p && p.input_id) byInput.set(p.input_id, p);
  }
  // Group by (block_id, kind) so each block gets a coherent legend entry.
  const traces = [];
  for (const ev of events) {
    if (!ev || !ev.kind || !EVENT_MARKER[ev.kind]) continue;
    const position = byInput.get(ev.input_id);
    if (!position || !position.price || !Array.isArray(position.price.values)) continue;
    // noRepeat=true: effective events only (no consecutive duplicates).
    //   - entries: use latched_indices (backend tracks state-change).
    //   - exits: backend mirrors fired for exits, so we filter fired_indices
    //     to bars where the position actually changed (exit had something to close).
    // noRepeat=false (default): all fired_indices (every bar the condition is true).
    let indices;
    if (noRepeat) {
      const isExit = ev.kind === 'long_exit' || ev.kind === 'short_exit';
      if (isExit) {
        const fired = Array.isArray(ev.fired_indices) ? ev.fired_indices : [];
        const posVals = Array.isArray(position.values) ? position.values : [];
        indices = fired.filter((i) => {
          if (i <= 0 || i >= posVals.length) return false;
          // Effective exit: position changed at this bar compared to previous.
          const prev = posVals[i - 1];
          const cur = posVals[i];
          return (typeof prev === 'number' && typeof cur === 'number' && prev !== cur);
        });
      } else {
        indices = Array.isArray(ev.latched_indices) ? ev.latched_indices : [];
      }
    } else {
      indices = Array.isArray(ev.fired_indices) ? ev.fired_indices : [];
    }
    if (indices.length === 0) continue;
    const style = EVENT_MARKER[ev.kind];
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

/**
 * Compute the clip summary banner rows, mirroring the iter-4 behaviour.
 * Extracted so the shell component can show it above both plots.
 */
export function buildClipSummary(result) {
  if (!result || !result.clipped) return null;
  const positions = Array.isArray(result.positions) ? result.positions : [];
  const rows = [];
  for (const p of positions) {
    const mask = Array.isArray(p.clipped_mask) ? p.clipped_mask : [];
    let count = 0;
    for (const b of mask) { if (b) count += 1; }
    if (count > 0) {
      rows.push({ instrument: `${p.input_id || '?'}`, count });
    }
  }
  return { rows };
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
    const capital = opts.capital || 1;
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
  const noRepeat = opts.noRepeat || false;
  const eventTraces = buildEventMarkerTraces(events, positions, dates, 'y2', { noRepeat });
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
