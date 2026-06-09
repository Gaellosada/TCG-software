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
 *     latched_indices, active_indices, target_entry_block_names}]`` —
 *     ``kind`` is ``"entry"`` or ``"exit"``. Color/direction comes from
 *     the sign of the originating block's signed weight (for entries) or
 *     the first targeted entry's weight (for exits). That mapping lives in
 *     the caller — we receive it via ``blockWeightSigns``.
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
// Reset markers use a neutral indigo and a distinctive star-diamond symbol
// so they read as "arming events" — visually different from triangle-based
// entry/exit markers. Sign is irrelevant for resets (signal-global), so the
// same style is used for all three sign keys.
const COLOR_RESET = '#6366f1';
const RESET_STYLE = { symbol: 'star-diamond', color: COLOR_RESET, open: false, label: 'reset' };

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
  reset: {
    1: RESET_STYLE,
    '-1': RESET_STYLE,
    0: RESET_STYLE,
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
/**
 * Build a block-id → display-name map from a v4 signal's rules.
 *
 * Display name falls back to a section-prefixed label using the block's
 * 1-based index WITHIN its section: ``"Entry 1"``, ``"Exit 2"``,
 * ``"Reset 1"``. Whitespace-only ``name`` is treated as empty and the
 * fallback is used.
 *
 * The fallback intentionally differs from the editor's cross-section
 * ``"Block N"`` (BlockHeader.jsx) — the chart benefits from explicit
 * section context for marker legend readability.
 */
export function buildBlockDisplayNameMap(rules) {
  const map = {};
  if (!rules || typeof rules !== 'object') return map;
  const sec = (list, label) => {
    if (!Array.isArray(list)) return;
    list.forEach((b, i) => {
      if (b && b.id) {
        const trimmed = typeof b.name === 'string' ? b.name.trim() : '';
        map[b.id] = trimmed || `${label} ${i + 1}`;
      }
    });
  };
  sec(rules.entries, 'Entry');
  sec(rules.exits, 'Exit');
  sec(rules.resets, 'Reset');
  return map;
}

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
    // v6: an exit may target several entries. For marker colouring we use
    // the sign of the FIRST target that resolves (deterministic by array
    // order). Mixed-sign targets still colour by the first; the per-event
    // label still names the exit block, so this is purely cosmetic.
    const targets = Array.isArray(x.target_entry_block_names)
      ? x.target_entry_block_names
      : [];
    let s = 0;
    for (const tgt of targets) {
      if (!tgt) continue;
      const hit = entries.find(
        (e) => e && e.name === tgt
          && Object.prototype.hasOwnProperty.call(entrySignById, e.id),
      );
      if (hit) {
        s = entrySignById[hit.id];
        break;
      }
    }
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
  { blockWeightSigns, blockDisplayNames } = {},
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

    // Reset events are signal-global (input_id === ''); they ride on the
    // first input that has price data so the marker still has somewhere
    // to land on the bottom subplot.
    const positionKey = ev.input_id === '' || ev.input_id == null
      ? (positions.find((p) => p && p.price && Array.isArray(p.price.values))?.input_id)
      : ev.input_id;
    const position = byInput.get(positionKey);
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
    const displayName = (blockDisplayNames && blockDisplayNames[ev.block_id])
      || (ev.block_id ? `block ${ev.block_id}` : '');
    const suffix = displayName ? ` • ${displayName}` : '';
    // Signal-global events (resets) omit the per-input bullet so the
    // legend doesn't read "reset •  • Reset 1".
    const inputBullet = ev.input_id ? ` • ${ev.input_id}` : '';
    const inputHover = ev.input_id ? ` on ${ev.input_id}` : '';
    const trace = {
      x: xs,
      y: ys,
      type: 'scatter',
      mode: 'markers',
      name: `${style.label}${inputBullet}${suffix}`,
      marker: {
        symbol: style.open ? `${style.symbol}-open` : style.symbol,
        color: style.color,
        size: 11,
        line: { color: style.color, width: 1.5 },
      },
      hovertemplate: `%{x}<br>${style.label}${inputHover}${suffix}<extra></extra>`,
      legendgroup: `events-${ev.block_id || ev.kind}`,
      showlegend: true,
    };
    if (yaxis) trace.yaxis = yaxis;
    traces.push(trace);
  }
  return traces;
}

/**
 * Format a short params suffix for an indicator trace label.
 * E.g. ``{ window: 3 }`` → ``"(window=3)"``, ``null`` → ``""``.
 * NOTE: no leading space — the caller concatenates directly onto the
 * indicator display name, so ``SMA(window=20)`` is the resulting form.
 */
function paramsLabel(paramsOverride) {
  if (!paramsOverride || typeof paramsOverride !== 'object') return '';
  const entries = Object.entries(paramsOverride);
  if (entries.length === 0) return '';
  const inner = entries.map(([k, v]) => `${k}=${v}`).join(', ');
  return `(${inner})`;
}

/**
 * Compose a parallel array of legend names for indicator traces.
 *
 * The decision to append ``" on <input_id>"`` is GROUP-AWARE: a suffix
 * is added only when the same ``(indicator_id, params_override)`` pair
 * appears more than once AND spans multiple distinct ``input_id``
 * values. Otherwise the name is just ``"<DisplayType>(<params>)"`` —
 * the input_id is omitted because it would be redundant.
 *
 * ``availableIndicators`` (optional) is the frontend hydrated registry
 * (`[{id, name, ...}, ...]`). When ``id`` matches we use the
 * user-facing display name; otherwise we fall back to the raw
 * ``indicator_id`` (kebab-case from the backend).
 *
 * @param {Array<object>} indicators        v4 ``result.indicators`` entries
 * @param {Array<object>} [availableIndicators]  registry from hydrateIndicators
 * @returns {string[]}                      parallel name array
 */
export function formatIndicatorTraceNames(indicators, availableIndicators) {
  if (!Array.isArray(indicators) || indicators.length === 0) return [];
  // Stable param-key by sorted-entry stringification so { a:1, b:2 } and
  // { b:2, a:1 } collide into the same group.
  const paramKey = (p) => {
    if (!p || typeof p !== 'object') return '';
    return Object.entries(p)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([k, v]) => `${k}=${v}`)
      .join(',');
  };
  // Group by (indicator_id, paramKey) → list of indices.
  const groups = new Map();
  indicators.forEach((ind, i) => {
    const key = `${ind?.indicator_id ?? ''}|${paramKey(ind?.params_override)}`;
    const arr = groups.get(key);
    if (arr) arr.push(i);
    else groups.set(key, [i]);
  });
  const lookupName = (id) => {
    if (!Array.isArray(availableIndicators)) return id;
    const hit = availableIndicators.find((d) => d && d.id === id);
    return (hit && hit.name) || id;
  };
  return indicators.map((ind, i) => {
    if (!ind) return '';
    const displayType = lookupName(ind.indicator_id);
    const params = paramsLabel(ind.params_override);
    const key = `${ind.indicator_id ?? ''}|${paramKey(ind.params_override)}`;
    const groupIdxs = groups.get(key) || [i];
    const groupInputs = new Set(groupIdxs.map((j) => indicators[j]?.input_id));
    const appendInput = groupIdxs.length >= 2 && groupInputs.size >= 2 && ind.input_id;
    const suffix = appendInput ? ` on ${ind.input_id}` : '';
    return `${displayType}${params}${suffix}`;
  });
}

/**
 * Build indicator traces for a plot. One line per indicator entry.
 * Colours cycle through TRACE_COLORS starting from the index AFTER the
 * input traces to avoid colour collisions.
 *
 * When the same indicator_id appears more than once (different params),
 * each instance gets its own trace with params in the label.
 *
 * @param {string} [yaxis]  Plotly yaxis ref (e.g. 'y2'). Defaults to 'y2'
 *                           for backward compat with the old bottom-plot
 *                           callers.
 */
export function buildIndicatorTraces(indicators, dates, colorOffset = 0, yaxis = 'y2', opts = {}) {
  if (!Array.isArray(indicators) || indicators.length === 0) return [];
  const filtered = indicators.filter((ind) => ind && Array.isArray(ind.series));
  // Group-aware naming must see all rows to decide whether to disambiguate
  // by input_id, so we compute names against the unfiltered set then index
  // into the filtered subset.
  const allNames = formatIndicatorTraceNames(indicators, opts.availableIndicators);
  return filtered.map((ind, i) => {
    const origIndex = indicators.indexOf(ind);
    return {
      x: dates,
      y: ind.series,
      type: 'scatter',
      mode: 'lines',
      name: allNames[origIndex] ?? '',
      line: {
        color: TRACE_COLORS[(colorOffset + i) % TRACE_COLORS.length],
        width: 1,
        dash: 'dot',
      },
      yaxis,
      connectgaps: false,
      hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      legendgroup: `ind-${ind.indicator_id}-${i}`,
    };
  });
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
 * @param {object}  [opts.signalRules] v4 ``rules`` ({entries, exits, resets})
 *        used to resolve per-block weight signs for marker colouring and
 *        per-block display names for marker legend/hover text. If omitted
 *        every marker falls back to the neutral style and the raw UUID
 *        block id is shown.
 * @param {Array<object>} [opts.availableIndicators]  hydrated indicator
 *        registry ({id, name, ...}); used to render legend labels with
 *        the user-facing display name instead of the kebab-case
 *        ``indicator_id``.
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

  // Group ownPanel indicators by indicator_id so multiple instances of the
  // same indicator (e.g. HV(20) and HV(100)) share one subplot. Each group
  // becomes one panel; group members render as distinct traces on it.
  const ownPanelGroups = [];
  {
    const byId = new Map();
    for (let i = 0; i < ownPanelWithData.length; i++) {
      const ind = ownPanelWithData[i];
      let g = byId.get(ind.indicator_id);
      if (!g) {
        g = { indicator_id: ind.indicator_id, memberIndices: [] };
        byId.set(ind.indicator_id, g);
        ownPanelGroups.push(g);
      }
      g.memberIndices.push(i);
    }
  }

  const domains = computeSubplotDomains(ownPanelGroups.length);
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
  const overlayTraces = buildIndicatorTraces(overlay, dates, bottomInputTraces.length, 'y2', {
    availableIndicators: opts.availableIndicators,
  });
  traces.push(...overlayTraces);

  const events = Array.isArray(result.events) ? result.events : [];
  const blockWeightSigns = opts.signalRules
    ? buildBlockWeightSignMap(opts.signalRules)
    : undefined;
  const blockDisplayNames = opts.signalRules
    ? buildBlockDisplayNameMap(opts.signalRules)
    : undefined;
  const eventTraces = buildEventMarkerTraces(events, positions, dates, 'y2', {
    blockWeightSigns,
    blockDisplayNames,
  });
  traces.push(...eventTraces);

  // --- Subplots 3..N: one per ownPanel indicator_id group ---
  // Offset colours past the price + overlay traces to avoid shared colours.
  const ownPanelColorOffset = bottomInputTraces.length + overlayTraces.length;
  const ownPanelNames = formatIndicatorTraceNames(ownPanelWithData, opts.availableIndicators);
  ownPanelGroups.forEach((g, gi) => {
    const axisRef = `y${gi + 3}`;
    for (const i of g.memberIndices) {
      const m = ownPanelWithData[i];
      traces.push({
        x: dates,
        y: m.series,
        type: 'scatter',
        mode: 'lines',
        name: ownPanelNames[i] ?? '',
        line: { color: TRACE_COLORS[(ownPanelColorOffset + i) % TRACE_COLORS.length], width: 1.5 },
        yaxis: axisRef,
        connectgaps: false,
        hovertemplate: '%{x}<br>%{y:,.4f}<extra></extra>',
      });
    }
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
      anchor: ownPanelGroups.length > 0
        ? `y${ownPanelGroups.length + 2}`
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

  // ownPanel y-axes — one per indicator_id group; title prefers the
  // user-facing display name from availableIndicators, falling back to
  // the raw indicator_id when no match.
  const registry = Array.isArray(opts.availableIndicators) ? opts.availableIndicators : [];
  ownPanelGroups.forEach((g, gi) => {
    const def = registry.find((d) => d && d.id === g.indicator_id);
    const title = (def && def.name) || g.indicator_id;
    lo[`yaxis${gi + 3}`] = {
      ...SUBPLOT_YAXIS_BASE,
      domain: domains[gi + 2],
      title: { text: title },
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
