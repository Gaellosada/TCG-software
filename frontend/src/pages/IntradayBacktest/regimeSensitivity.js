// Regime-sensitivity grouping (A2 / W3-P2).
//
// PURE POST-PROCESSING over the existing intraday backtest run response — no
// engine/schema/serializer change. w3_f2_1/w3_f2_2 reports: when regime
// emit-signals or regime-driven side is on, each `days[]` entry carries a
// per-day `regime` object. Two shapes exist depending on which F2 half is
// active:
//   - F2.2 decision mode (`side_mode='regime_driven'`): `regime.state`,
//     `regime.side`, `regime.asof`, `regime.gate`, `regime.signals.{h20,
//     h30,h100,vvix}` — the as-of signals the decision consumed.
//   - F2.1 emit-only mode (`emit_signals` on, `side_mode` off): `regime` IS
//     the raw per-day signal map directly (`regime.{h20,h30,h100,vvix}`), no
//     state/side/asof/gate.
// This module tolerates BOTH shapes for the scatter (signal) data, but only
// the decision shape carries `state`, so per-state bucketing only ever
// populates when F2.2 is active — an emit-only run legitimately produces all
// buckets at N=0 while still feeding the scatter plots.
//
// Only TRADED days (a day with a finite `pnl.total_pnl_usd`) contribute to
// bucket stats and scatter points — same convention as weekdayAttribution.js.
// A day whose signal is missing/null is excluded from that scatter series
// entirely (never plotted as 0) — an absent RV/VVIX reading is not a zero.

export const REGIME_STATES = ['hvol_on', 'hvol_off', 'extremely_low', 'fallback'];

export const REGIME_STATE_LABELS = {
  hvol_on: 'HVOL on',
  hvol_off: 'HVOL off',
  extremely_low: 'Extremely low',
  fallback: 'Fallback',
};

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function hasRegimeKey(day) {
  return day !== null && typeof day === 'object' && Object.prototype.hasOwnProperty.call(day, 'regime');
}

// Tolerates both the F2.2 decision shape (`regime.signals`) and the F2.1
// emit-only shape (`regime` itself is the signal map).
function extractSignals(regime) {
  if (!regime || typeof regime !== 'object') return null;
  if (regime.signals && typeof regime.signals === 'object') return regime.signals;
  return regime;
}

function signalValue(signals, key) {
  if (!signals || typeof signals !== 'object') return null;
  const v = signals[key];
  return isFiniteNumber(v) ? v : null;
}

const EMPTY_RESULT = {
  available: false,
  buckets: [],
  scatter: { vvix: [], rvSpread: [] },
};

/**
 * Join each day's PnL with its regime state/signals and aggregate.
 *
 * @param {Array<object|null|undefined>} days - the run response's `days[]`.
 * @returns {{
 *   available: boolean,
 *   buckets: Array<{ state: string, label: string, n: number, sumUsd: number,
 *     meanUsd: number|null, winRate: number|null }>,
 *   scatter: {
 *     vvix: Array<{ x: number, y: number, date: string }>,
 *     rvSpread: Array<{ x: number, y: number, date: string }>,
 *   },
 * }} `available` is false when no day in the input carries a `regime` key at
 *   all (regime was off for this run) — callers should render an
 *   enable-regime hint rather than an empty chart in that case. When
 *   available, `buckets` always has exactly 4 entries (REGIME_STATES order),
 *   zeroed where a state has no traded days.
 */
export function computeRegimeSensitivity(days) {
  const list = Array.isArray(days) ? days : [];
  if (!list.some(hasRegimeKey)) return EMPTY_RESULT;

  const raw = Object.fromEntries(
    REGIME_STATES.map((s) => [s, { n: 0, sumUsd: 0, wins: 0 }])
  );
  const vvixScatter = [];
  const rvSpreadScatter = [];

  for (const day of list) {
    if (!day || typeof day !== 'object') continue;
    const pnl = day.pnl;
    if (!pnl || typeof pnl !== 'object' || !isFiniteNumber(pnl.total_pnl_usd)) continue;
    const usd = pnl.total_pnl_usd;

    const regime = day.regime;
    const state = regime && typeof regime === 'object' ? regime.state : null;
    if (typeof state === 'string' && REGIME_STATES.includes(state)) {
      const b = raw[state];
      b.n += 1;
      b.sumUsd += usd;
      if (usd > 0) b.wins += 1;
    }

    const signals = extractSignals(regime);
    const vvix = signalValue(signals, 'vvix');
    if (vvix !== null) vvixScatter.push({ x: vvix, y: usd, date: day.date });

    const h20 = signalValue(signals, 'h20');
    const h100 = signalValue(signals, 'h100');
    if (h20 !== null && h100 !== null) {
      rvSpreadScatter.push({ x: h20 - h100, y: usd, date: day.date });
    }
  }

  const buckets = REGIME_STATES.map((s) => {
    const b = raw[s];
    return {
      state: s,
      label: REGIME_STATE_LABELS[s],
      n: b.n,
      sumUsd: b.sumUsd,
      meanUsd: b.n > 0 ? b.sumUsd / b.n : null,
      winRate: b.n > 0 ? b.wins / b.n : null,
    };
  });

  return { available: true, buckets, scatter: { vvix: vvixScatter, rvSpread: rvSpreadScatter } };
}
