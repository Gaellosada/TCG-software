/**
 * Pure helpers for the Results 2-plot view (iter-5 ask #6).
 *
 * These helpers translate the v3 + iter-5 signals compute response into
 * Plotly trace arrays, one helper per plot (top / bottom). They are
 * kept outside the React components so Vitest can exercise the trace
 * construction without rendering Plotly.
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
 * @returns Plotly trace objects, one per input that has a price.
 */
export function buildInputTraces(positions, dates) {
  const traces = [];
  positions.forEach((p, idx) => {
    if (!p || !p.price || !Array.isArray(p.price.values)) return;
    const color = TRACE_COLORS[idx % TRACE_COLORS.length];
    traces.push({
      x: dates,
      y: p.price.values,
      type: 'scatter',
      mode: 'lines',
      name: `${p.input_id} — ${p.price.label || 'price'}`,
      line: { color, width: 1.5 },
      connectgaps: false,
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      legendgroup: `input-${p.input_id}`,
    });
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
 * Top plot: inputs + realized P&L (one aggregate line, right y-axis).
 *
 * Returns ``{traces, layoutOverrides, hasData}``. ``hasData`` is false
 * when there is neither any price input nor a PnL series.
 */
export function buildTopPlot(result) {
  if (!result || !Array.isArray(result.timestamps) || result.timestamps.length === 0) {
    return { traces: [], layoutOverrides: {}, hasData: false };
  }
  const positions = Array.isArray(result.positions) ? result.positions : [];
  const dates = toDates(result.timestamps);
  const inputTraces = buildInputTraces(positions, dates);

  const pnlSeries = aggregateRealizedPnl(result.realized_pnl, result.timestamps.length);
  const traces = [...inputTraces];
  const lo = {
    showlegend: true,
    legend: { orientation: 'h', y: -0.2 },
    xaxis: { title: { text: '' } },
    yaxis: { title: { text: 'price' } },
  };

  if (pnlSeries) {
    traces.push({
      x: dates,
      y: pnlSeries,
      type: 'scatter',
      mode: 'lines',
      name: 'realized P&L',
      line: { color: '#e4e8f0', width: 2, dash: 'solid' },
      yaxis: 'y2',
      hovertemplate: '%{x}<br>P&L %{y:,.4f}<extra></extra>',
      legendgroup: 'pnl',
    });
    lo.yaxis2 = {
      overlaying: 'y',
      side: 'right',
      showgrid: false,
      title: { text: 'realized P&L' },
      zeroline: true,
      zerolinecolor: 'rgba(155,163,184,0.3)',
    };
  }

  return {
    traces,
    layoutOverrides: lo,
    hasData: traces.length > 0,
  };
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
 */
export function buildEventMarkerTraces(events, positions, dates) {
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
    const indices = Array.isArray(ev.latched_indices) && ev.latched_indices.length > 0
      ? ev.latched_indices
      : (Array.isArray(ev.fired_indices) ? ev.fired_indices : []);
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
    traces.push({
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
    });
  }
  return traces;
}

/**
 * Build indicator traces for the bottom plot. One line per indicator
 * entry. Colours cycle through TRACE_COLORS starting from the index
 * AFTER the input traces to avoid colour collisions.
 */
export function buildIndicatorTraces(indicators, dates, colorOffset = 0) {
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
      yaxis: 'y2',
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
 * Bottom plot: inputs (price) + overlay indicators (right axis) + event
 * markers (on the price axis). Indicators with ownPanel: true are
 * excluded — they render in dedicated panels.
 */
export function buildBottomPlot(result) {
  if (!result || !Array.isArray(result.timestamps) || result.timestamps.length === 0) {
    return { traces: [], layoutOverrides: {}, hasData: false };
  }
  const positions = Array.isArray(result.positions) ? result.positions : [];
  const dates = toDates(result.timestamps);
  const inputTraces = buildInputTraces(positions, dates);
  const { overlay } = partitionIndicators(
    Array.isArray(result.indicators) ? result.indicators : [],
  );
  const indicatorTraces = buildIndicatorTraces(overlay, dates, inputTraces.length);
  const events = Array.isArray(result.events) ? result.events : [];
  const eventTraces = buildEventMarkerTraces(events, positions, dates);

  const traces = [...inputTraces, ...indicatorTraces, ...eventTraces];
  const lo = {
    showlegend: true,
    legend: { orientation: 'h', y: -0.2 },
    xaxis: { title: { text: '' } },
    yaxis: { title: { text: 'price' } },
  };
  if (indicatorTraces.length > 0) {
    lo.yaxis2 = {
      overlaying: 'y',
      side: 'right',
      showgrid: false,
      title: { text: 'indicator' },
    };
  }

  return {
    traces,
    layoutOverrides: lo,
    hasData: traces.length > 0,
  };
}

/**
 * Build dedicated panel plots for indicators with ownPanel: true.
 * Each panel shows the input prices on the left axis and the indicator
 * on the right axis.
 */
export function buildOwnPanelPlots(result) {
  if (!result || !Array.isArray(result.timestamps) || result.timestamps.length === 0) {
    return [];
  }
  const { ownPanel } = partitionIndicators(
    Array.isArray(result.indicators) ? result.indicators : [],
  );
  if (ownPanel.length === 0) return [];

  const positions = Array.isArray(result.positions) ? result.positions : [];
  const dates = toDates(result.timestamps);

  return ownPanel.map((ind, i) => {
    const inputTraces = buildInputTraces(positions, dates);
    const indTrace = {
      x: dates,
      y: ind.series,
      type: 'scatter',
      mode: 'lines',
      name: `ind: ${ind.indicator_id}${ind.input_id ? ` • ${ind.input_id}` : ''}`,
      line: { color: TRACE_COLORS[i % TRACE_COLORS.length], width: 1.5 },
      yaxis: 'y2',
      connectgaps: false,
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
    };
    return {
      traces: [...inputTraces, indTrace],
      layoutOverrides: {
        showlegend: true,
        legend: { orientation: 'h', y: -0.2 },
        xaxis: { title: { text: '' } },
        yaxis: { title: { text: 'price' } },
        yaxis2: { overlaying: 'y', side: 'right', showgrid: false, title: { text: ind.indicator_id } },
      },
      hasData: Array.isArray(ind.series) && ind.series.length > 0,
      title: ind.indicator_id,
      downloadFilename: `signal-indicator-${ind.indicator_id}`,
    };
  });
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
