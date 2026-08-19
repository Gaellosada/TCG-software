import { describe, it, expect } from 'vitest';
import { computeRegimeSensitivity, REGIME_STATES } from './regimeSensitivity';

// Decision-mode day (F2.2 side_mode='regime_driven'): `regime.signals` nests
// the as-of signals under state/side/asof/gate.
function decisionDay(date, usd, state, signals = {}, extra = {}) {
  return {
    date,
    status: 'ok',
    pnl: { option_pnl_pts: usd / 10, hedge_pnl_pts: 0, total_pnl_pts: usd / 10, total_pnl_usd: usd },
    regime: {
      state,
      side: state === 'hvol_on' ? 'long' : state === 'hvol_off' ? 'short' : 'flat',
      asof: 20250131,
      gate: null,
      signals: { h20: null, h30: null, h100: null, vvix: null, ...signals },
    },
    ...extra,
  };
}

function flatDay(date, state, signals = {}) {
  return {
    date, status: 'skipped', skip_reason: 'regime_flat', pnl: null,
    regime: { state, side: 'flat', asof: 20250131, gate: null,
      signals: { h20: null, h30: null, h100: null, vvix: null, ...signals } },
  };
}

describe('computeRegimeSensitivity — no regime data', () => {
  it('returns an unavailable shape for an empty days array', () => {
    const result = computeRegimeSensitivity([]);
    expect(result.available).toBe(false);
    expect(result.buckets).toEqual([]);
  });

  it('returns unavailable when no day carries a regime key at all (regime off)', () => {
    const days = [
      { date: '2025-02-03', status: 'ok', pnl: { total_pnl_usd: 100, total_pnl_pts: 10 } },
      { date: '2025-02-04', status: 'ok', pnl: { total_pnl_usd: -50, total_pnl_pts: -5 } },
    ];
    const result = computeRegimeSensitivity(days);
    expect(result.available).toBe(false);
  });

  it('returns unavailable for undefined/null input without throwing', () => {
    expect(() => computeRegimeSensitivity(undefined)).not.toThrow();
    expect(() => computeRegimeSensitivity(null)).not.toThrow();
    expect(computeRegimeSensitivity(undefined).available).toBe(false);
  });
});

describe('computeRegimeSensitivity — per-state bucketing', () => {
  it('buckets a day of each state into the right bucket with correct N', () => {
    const days = [
      decisionDay('2025-02-03', 100, 'hvol_on'),
      decisionDay('2025-02-04', -50, 'hvol_off'),
      flatDay('2025-02-05', 'extremely_low'),
      decisionDay('2025-02-06', 30, 'fallback'),
    ];
    // extremely_low day is flat (no trade => no pnl) so it contributes N=0 there.
    const result = computeRegimeSensitivity(days);
    expect(result.available).toBe(true);
    const byState = Object.fromEntries(result.buckets.map((b) => [b.state, b]));
    expect(REGIME_STATES).toEqual(['hvol_on', 'hvol_off', 'extremely_low', 'fallback']);
    expect(byState.hvol_on.n).toBe(1);
    expect(byState.hvol_on.sumUsd).toBeCloseTo(100);
    expect(byState.hvol_off.n).toBe(1);
    expect(byState.hvol_off.sumUsd).toBeCloseTo(-50);
    expect(byState.extremely_low.n).toBe(0); // flat day has no pnl -> excluded from stats
    expect(byState.fallback.n).toBe(1);
    expect(byState.fallback.sumUsd).toBeCloseTo(30);
  });

  it('computes mean and win rate per state bucket', () => {
    const days = [
      decisionDay('2025-02-03', 100, 'hvol_on'),
      decisionDay('2025-02-10', -20, 'hvol_on'),
    ];
    const result = computeRegimeSensitivity(days);
    const hvolOn = result.buckets.find((b) => b.state === 'hvol_on');
    expect(hvolOn.n).toBe(2);
    expect(hvolOn.sumUsd).toBeCloseTo(80);
    expect(hvolOn.meanUsd).toBeCloseTo(40);
    expect(hvolOn.winRate).toBeCloseTo(0.5);
  });

  it('excludes non-traded (flat/skipped, no pnl) days from bucket stats', () => {
    const days = [
      decisionDay('2025-02-03', 100, 'hvol_on'),
      flatDay('2025-02-04', 'extremely_low'),
    ];
    const result = computeRegimeSensitivity(days);
    const extremelyLow = result.buckets.find((b) => b.state === 'extremely_low');
    expect(extremelyLow.n).toBe(0);
    expect(extremelyLow.meanUsd).toBeNull();
    expect(extremelyLow.winRate).toBeNull();
  });

  it('ignores an unrecognized/malformed state value defensively', () => {
    const days = [decisionDay('2025-02-03', 100, 'not_a_real_state')];
    const result = computeRegimeSensitivity(days);
    const total = result.buckets.reduce((acc, b) => acc + b.n, 0);
    expect(total).toBe(0);
  });

  it('tolerates a day whose regime value is null (present key, no data)', () => {
    const days = [
      { date: '2025-02-03', status: 'ok', pnl: { total_pnl_usd: 100, total_pnl_pts: 10 }, regime: null },
      decisionDay('2025-02-04', 50, 'hvol_on'),
    ];
    expect(() => computeRegimeSensitivity(days)).not.toThrow();
    const result = computeRegimeSensitivity(days);
    expect(result.available).toBe(true);
    expect(result.buckets.find((b) => b.state === 'hvol_on').n).toBe(1);
  });
});

describe('computeRegimeSensitivity — scatter data', () => {
  it('builds a vvix scatter point per day with a finite vvix signal', () => {
    const days = [
      decisionDay('2025-02-03', 100, 'hvol_on', { vvix: 95.3 }),
      decisionDay('2025-02-04', -50, 'hvol_off', { vvix: 110.2 }),
    ];
    const result = computeRegimeSensitivity(days);
    expect(result.scatter.vvix).toEqual([
      { x: 95.3, y: 100, date: '2025-02-03' },
      { x: 110.2, y: -50, date: '2025-02-04' },
    ]);
  });

  it('excludes days with a null vvix signal from the vvix scatter (not plotted as 0)', () => {
    const days = [
      decisionDay('2025-02-03', 100, 'hvol_on', { vvix: null }),
      decisionDay('2025-02-04', -50, 'hvol_off', { vvix: 110.2 }),
    ];
    const result = computeRegimeSensitivity(days);
    expect(result.scatter.vvix).toHaveLength(1);
    expect(result.scatter.vvix[0].x).toBeCloseTo(110.2);
  });

  it('builds an RV term-structure (H20-H100) spread scatter point per day', () => {
    const days = [
      decisionDay('2025-02-03', 100, 'hvol_on', { h20: 0.2, h100: 0.15 }),
      decisionDay('2025-02-04', -50, 'hvol_off', { h20: 0.12, h100: 0.18 }),
    ];
    const result = computeRegimeSensitivity(days);
    expect(result.scatter.rvSpread).toHaveLength(2);
    expect(result.scatter.rvSpread[0].x).toBeCloseTo(0.05);
    expect(result.scatter.rvSpread[0].y).toBe(100);
    expect(result.scatter.rvSpread[0].date).toBe('2025-02-03');
    expect(result.scatter.rvSpread[1].x).toBeCloseTo(-0.06);
  });

  it('excludes a day from the RV spread scatter if either leg (h20/h100) is missing', () => {
    const days = [
      decisionDay('2025-02-03', 100, 'hvol_on', { h20: 0.2, h100: null }),
      decisionDay('2025-02-04', -50, 'hvol_off', { h20: null, h100: 0.18 }),
      decisionDay('2025-02-05', 30, 'fallback', { h20: 0.1, h100: 0.1 }),
    ];
    const result = computeRegimeSensitivity(days);
    expect(result.scatter.rvSpread).toHaveLength(1);
    expect(result.scatter.rvSpread[0].date).toBe('2025-02-05');
  });

  it('excludes a non-traded day from scatter data even if signals are present', () => {
    const days = [flatDay('2025-02-04', 'extremely_low', { vvix: 200, h20: 0.01, h100: 0.03 })];
    const result = computeRegimeSensitivity(days);
    expect(result.scatter.vvix).toHaveLength(0);
    expect(result.scatter.rvSpread).toHaveLength(0);
  });

  it('also joins signals from the F2.1 emit-only shape (regime.{h20,...} directly, no .signals wrapper)', () => {
    const days = [
      {
        date: '2025-02-03', status: 'ok',
        pnl: { total_pnl_usd: 75, total_pnl_pts: 7.5 },
        regime: { h20: 0.22, h30: 0.2, h100: 0.14, vvix: 88.0 },
      },
    ];
    const result = computeRegimeSensitivity(days);
    expect(result.available).toBe(true);
    expect(result.scatter.vvix).toEqual([{ x: 88.0, y: 75, date: '2025-02-03' }]);
    expect(result.scatter.rvSpread[0].x).toBeCloseTo(0.08);
    // emit-only shape carries no `state` -> no bucket gets it.
    const total = result.buckets.reduce((acc, b) => acc + b.n, 0);
    expect(total).toBe(0);
  });

  it('tolerates null/malformed entries mixed into the days array', () => {
    const days = [null, undefined, {}, decisionDay('2025-02-05', 200, 'hvol_on', { vvix: 90 })];
    expect(() => computeRegimeSensitivity(days)).not.toThrow();
    const result = computeRegimeSensitivity(days);
    expect(result.scatter.vvix).toHaveLength(1);
  });
});
