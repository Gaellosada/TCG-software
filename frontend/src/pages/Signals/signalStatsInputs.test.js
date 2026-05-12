// Unit tests for buildSignalStatsInputs — the bridge between a v4
// signals compute response and the Statistics endpoint's equity input.
//
// The function must return null for every shape the backend would
// reject, so SignalsPage can simply skip mounting <Statistics> rather
// than surface a 400 inside the panel.

import { describe, it, expect } from 'vitest';
import { buildSignalStatsInputs } from './signalStatsInputs';

// Two timestamps, one input series — gives a 2-point equity curve.
const TS = [1704067200000, 1704153600000]; // 2024-01-01, 2024-01-02 UTC

function makeResult(realizedPnl) {
  return { timestamps: TS, realized_pnl: realizedPnl };
}

describe('buildSignalStatsInputs', () => {
  it('returns {dates, equity} for a well-formed result', () => {
    const out = buildSignalStatsInputs(makeResult([[0.0, 0.10]]), 1000);
    expect(out).not.toBeNull();
    expect(out.dates).toEqual([20240101, 20240102]);
    // equity = capital + pnl_fraction * capital
    expect(out.equity).toEqual([1000, 1100]);
  });

  it('returns null when result is missing', () => {
    expect(buildSignalStatsInputs(null, 1000)).toBeNull();
    expect(buildSignalStatsInputs(undefined, 1000)).toBeNull();
  });

  it('returns null when timestamps array is missing', () => {
    expect(buildSignalStatsInputs({ realized_pnl: [[0.0, 0.1]] }, 1000)).toBeNull();
  });

  it('returns null when realized_pnl is missing or empty', () => {
    expect(buildSignalStatsInputs({ timestamps: TS }, 1000)).toBeNull();
    expect(buildSignalStatsInputs({ timestamps: TS, realized_pnl: [] }, 1000)).toBeNull();
  });

  it('returns null when equity hits zero (total loss)', () => {
    // pnl fraction of -1 at idx 1 → equity = capital + (-1)*capital = 0
    const out = buildSignalStatsInputs(makeResult([[0.0, -1.0]]), 1000);
    expect(out).toBeNull();
  });

  it('returns null when equity goes negative (>100% loss)', () => {
    const out = buildSignalStatsInputs(makeResult([[0.0, -1.5]]), 1000);
    expect(out).toBeNull();
  });

  it('returns null when realized_pnl contains NaN propagating to equity', () => {
    // aggregateRealizedPnl skips non-finite values, so a single NaN at
    // idx 1 makes that bucket sum to 0 (still finite). To force a NaN
    // in equity we need EVERY series at idx 1 to be non-finite, which
    // aggregateRealizedPnl returns as anyFinite=false → null.
    // Either way the result is null — that's the desired contract.
    const out = buildSignalStatsInputs(makeResult([[Number.NaN, Number.NaN]]), 1000);
    expect(out).toBeNull();
  });

  it('returns null when capital is zero (equity stays at 0)', () => {
    // capital=0 produces equity = 0 + v*0 = 0 for every point → guard rejects.
    const out = buildSignalStatsInputs(makeResult([[0.0, 0.1]]), 0);
    expect(out).toBeNull();
  });

  it('returns null when timestamps length < 2', () => {
    const out = buildSignalStatsInputs(
      { timestamps: [TS[0]], realized_pnl: [[0.0]] },
      1000,
    );
    expect(out).toBeNull();
  });
});
