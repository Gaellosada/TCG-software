import { useState, useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
import { useBasketSeries } from '../../hooks/marketQueries';
import { getBasketSeries } from '../../api/data';
import { queryKeys } from '../../queryKeys';
import useTheme from '../../hooks/useTheme';
import Chart from '../../components/Chart';
import { TRACE_COLORS, getChartColors } from '../../utils/chartTheme';
import { formatDateInt } from '../../utils/format';
import styles from './ChartBase.module.css';

// Default exploration window for a basket (baskets carry no inherent date
// range — D3).  ~5 years back from today, matching the platform's standard
// long-history default.  The user can widen/narrow via the date pickers; an
// option_stream-leg basket REQUIRES a window, so we prefill one.
function defaultRange() {
  const end = new Date();
  const start = new Date();
  start.setFullYear(start.getFullYear() - 5);
  const iso = (d) => d.toISOString().slice(0, 10);
  return { start: iso(start), end: iso(end) };
}

// Build the discriminated wire descriptor for a SINGLE leg, as a one-leg
// inline basket, so each per-leg trace reuses the same compute endpoint.
function singleLegBasket(assetClass, leg) {
  return { kind: 'inline', asset_class: assetClass, legs: [leg] };
}

// Stable module-level ``combine`` for the per-leg useQueries — extracting just
// the data array. A stable function reference lets TanStack memoise the
// combined result (recomputed only when an underlying query result changes),
// so the traces useMemo doesn't re-run on every render (N2).
const combineLegData = (results) => results.map((r) => r.data ?? null);

// A short human label for a leg, derived from its instrument ref.
function legLabel(leg, i) {
  const inst = leg?.instrument || {};
  if (inst.type === 'spot') return inst.instrument_id || inst.collection || `Leg ${i + 1}`;
  if (inst.type === 'continuous') return `${inst.collection} (cont)`;
  if (inst.type === 'option_stream') return `${inst.collection} ${inst.option_type || ''}`.trim();
  return `Leg ${i + 1}`;
}

/**
 * Explore a basket's composite series on the SHARED Chart.
 *
 * ``basket`` is the discriminated descriptor the BE expects:
 *   { kind:'saved', basket_id }  OR  { kind:'inline', asset_class, legs }
 * ``name`` is a display label; ``legs`` (optional) are the inline legs used
 * for the per-leg breakdown overlay (D1).  For a saved basket the legs are
 * not embedded in the descriptor, so the per-leg toggle is only offered when
 * legs are supplied by the caller.
 */
function BasketChart({ basket, name, assetClass, legs }) {
  const theme = useTheme();
  const colors = getChartColors(theme);

  const [{ start, end }, setRange] = useState(defaultRange);
  const [showLegs, setShowLegs] = useState(false);

  const { data, loading, error } = useBasketSeries(basket, { start, end });

  // Per-leg traces (D1) — one single-leg sub-basket fetch per leg, only when
  // the breakdown toggle is on AND we have the inline legs to decompose.
  const canBreakdown = Array.isArray(legs) && legs.length > 1 && !!assetClass;
  // ``combine`` lets TanStack hand back a STABLE array of just the leg data
  // (recomputed only when an underlying query result actually changes), so the
  // traces ``useMemo`` below doesn't recompute on every render from a fresh
  // useQueries array identity (N2).
  const legData = useQueries({
    queries:
      showLegs && canBreakdown
        ? legs.map((leg) => {
            const sub = singleLegBasket(assetClass, leg);
            return {
              queryKey: queryKeys.market.basketSeries(sub, { start, end, field: 'close' }),
              queryFn: ({ signal }) =>
                getBasketSeries(sub, { start, end, field: 'close', signal }),
              enabled: !!start && !!end,
            };
          })
        : [],
    combine: combineLegData,
  });

  const traces = useMemo(() => {
    if (!data || !data.dates || data.dates.length === 0) return [];
    const x = data.dates.map(formatDateInt);
    const t = [
      {
        x,
        y: data.values,
        type: 'scatter',
        mode: 'lines',
        name: name || 'Basket',
        line: { color: TRACE_COLORS[0], width: 1.5 },
        hovertemplate: '%{x}<br>Value: %{y:,.2f}<extra></extra>',
      },
    ];
    if (showLegs && canBreakdown) {
      legData.forEach((ld, i) => {
        if (!ld || !ld.dates || ld.dates.length === 0) return;
        t.push({
          x: ld.dates.map(formatDateInt),
          y: ld.values,
          type: 'scatter',
          mode: 'lines',
          name: legLabel(legs[i], i),
          line: { color: TRACE_COLORS[(i + 1) % TRACE_COLORS.length], width: 1, dash: 'dot' },
          hovertemplate: `%{x}<br>${legLabel(legs[i], i)}: %{y:,.2f}<extra></extra>`,
        });
      });
    }
    return t;
  }, [data, name, showLegs, canBreakdown, legData, legs]);

  const layoutOverrides = useMemo(
    () => ({
      yaxis: {
        title: { text: 'Value', font: { size: 11, color: colors.secondaryFont } },
        domain: [0, 1.0],
      },
    }),
    [colors],
  );

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>{name || 'Basket'} — Composite</h2>
        {data && data.dates && data.dates.length > 0 && (
          <span className={styles.meta}>
            {data.dates.length.toLocaleString()} bars
            &nbsp;&middot;&nbsp;
            {formatDateInt(data.dates[0])} to {formatDateInt(data.dates[data.dates.length - 1])}
          </span>
        )}
      </div>

      <div className={styles.controls}>
        <label className={styles.controlLabel}>
          Start
          <input
            type="date"
            className={styles.select}
            value={start}
            max={end}
            onChange={(e) => setRange((r) => ({ ...r, start: e.target.value }))}
          />
        </label>
        <label className={styles.controlLabel}>
          End
          <input
            type="date"
            className={styles.select}
            value={end}
            min={start}
            onChange={(e) => setRange((r) => ({ ...r, end: e.target.value }))}
          />
        </label>
        {canBreakdown && (
          <label className={styles.controlLabel} style={{ flexDirection: 'row', alignItems: 'center', gap: '6px' }}>
            <input
              type="checkbox"
              checked={showLegs}
              onChange={(e) => setShowLegs(e.target.checked)}
            />
            Show legs
          </label>
        )}
      </div>

      <div className={styles.chartCard}>
        {loading ? (
          <div className={styles.status}>Loading basket series...</div>
        ) : error ? (
          <div className={styles.error}>Failed to load basket: {error.message}</div>
        ) : !data || !data.dates || data.dates.length === 0 ? (
          <div className={styles.status}>No basket series data available for this range.</div>
        ) : (
          <Chart
            traces={traces}
            layoutOverrides={layoutOverrides}
            className={styles.chartWrapper}
            downloadFilename={`basket-${(name || 'composite').replace(/\s+/g, '_')}`}
          />
        )}
      </div>
    </div>
  );
}

export default BasketChart;
