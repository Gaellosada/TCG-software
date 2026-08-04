// Per-instrument data_source wire-field tests for the portfolio compute body.
//
// New model (immutable per-instrument source): a source is chosen ONCE, at add
// time, and rides each LEAF as its own ``dataSource``. There is NO page/run
// default and NO fold — the builder takes no ``dataSource`` argument. Invariants:
//   1. v1 (and undefined) emit NO ``data_source`` key anywhere — a body with no
//      v2 leaf is byte-identical to a pre-feature payload (the backend hashes the
//      whole body for its result cache; an unconditional "v1" would invalidate
//      every existing entry).
//   2. A leaf emits ``data_source:"v2"`` iff its OWN source is v2.
//   3. There is NO top-level ``data_source`` body field, ever.

import { describe, it, expect } from 'vitest';
import { buildPortfolioComputeBody } from './computeBodyBuilder';

function childDoc() {
  return {
    id: 'child-1',
    name: 'Child',
    category: 'RESEARCH',
    kind: 'pure',
    rebalance: 'monthly',
    legs: [
      // A child whose leaf carries its OWN persisted v2 source.
      { label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100, dataSource: 'v2' },
    ],
  };
}

const resolvePortfolio = (id) => (id === 'child-1' ? childDoc() : null);

const pureLegs = [
  { id: 1, label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 },
];
const composedLegs = [
  { id: 1, label: 'BuildingBlock', type: 'portfolio', portfolioId: 'child-1', weight: 60 },
];

const baseArgs = {
  rebalance: 'none',
  start: '2020-01-01',
  end: '2021-01-01',
  availableIndicators: [],
};

describe('buildPortfolioComputeBody — per-instrument data_source', () => {
  it('never emits a top-level data_source (per-instrument, not a run-level field)', () => {
    const v1 = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs }).body;
    const legsV2 = [{ ...pureLegs[0], dataSource: 'v2' }];
    const v2 = buildPortfolioComputeBody({ ...baseArgs, legs: legsV2 }).body;
    expect('data_source' in v1).toBe(false);
    expect('data_source' in v2).toBe(false);
  });

  it('omits data_source on a v1 leaf and on an unset leaf', () => {
    const a = buildPortfolioComputeBody({ ...baseArgs, legs: [{ ...pureLegs[0], dataSource: 'v1' }] }).body;
    const b = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs }).body;
    expect('data_source' in a.legs.SPX).toBe(false);
    expect('data_source' in b.legs.SPX).toBe(false);
  });

  it('a v1 leaf body is byte-identical to an unset leaf body', () => {
    const a = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs }).body;
    const b = buildPortfolioComputeBody({ ...baseArgs, legs: [{ ...pureLegs[0], dataSource: 'v1' }] }).body;
    expect(JSON.stringify(b)).toBe(JSON.stringify(a));
  });

  it('emits data_source:"v2" on a leaf whose OWN source is v2', () => {
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs: [{ ...pureLegs[0], dataSource: 'v2' }] });
    expect(body.legs.SPX.data_source).toBe('v2');
  });

  // The load-bearing per-instrument case: one flat portfolio, a v1 leg + a v2 leg.
  it('emits data_source:"v2" on the v2 leg and NO key on the v1 leg', () => {
    const legs = [
      { id: 1, label: 'A', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50, dataSource: 'v1' },
      { id: 2, label: 'B', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50, dataSource: 'v2' },
    ];
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs });
    expect('data_source' in body.legs.A).toBe(false);
    expect(body.legs.B.data_source).toBe('v2');
    expect('data_source' in body).toBe(false); // still no top-level field
  });

  it('applies the per-leaf source to continuous and option_stream leaves', () => {
    const legs = [
      {
        id: 1, label: 'C', type: 'continuous', collection: 'FUT_SP_500',
        strategy: 'front_month', adjustment: 'none', weight: 50, dataSource: 'v2',
      },
      {
        id: 2, label: 'O', type: 'option_stream', collection: 'OPT_SP_500',
        option_type: 'P', cycle: 'W3 Friday', maturity: { kind: 'nearest' },
        selection: { kind: 'atm' }, stream: 'close', weight: 50, dataSource: 'v1',
      },
    ];
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs });
    expect(body.legs.C.data_source).toBe('v2');
    expect('data_source' in body.legs.O).toBe(false);
  });

  it('inlines a composed child that carries its per-leaf source (no body-level default)', () => {
    const { body, brokenRefs } = buildPortfolioComputeBody({
      ...baseArgs, legs: composedLegs, resolvePortfolio,
    });
    expect(brokenRefs).toEqual([]);
    expect('data_source' in body).toBe(false);                          // no top-level
    // A composed leg carries no source of its own, so the child sub-body has NO
    // body-level default …
    expect('data_source' in body.legs.BuildingBlock.portfolio).toBe(false);
    // … but the child's leaf keeps its OWN persisted v2 source.
    expect(body.legs.BuildingBlock.portfolio.legs.SPX.data_source).toBe('v2');
  });

  it('changing a leaf source changes the serialized body — the backend cache key differs', () => {
    const v1 = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs }).body;
    const v2 = buildPortfolioComputeBody({ ...baseArgs, legs: [{ ...pureLegs[0], dataSource: 'v2' }] }).body;
    expect(JSON.stringify(v1)).not.toBe(JSON.stringify(v2));
  });

  it('a signal leg sub-body carries NO top-level data_source (source rides its inputs)', () => {
    const legs = [{
      id: 1,
      label: 'Sig',
      type: 'signal',
      weight: 100,
      signalSpec: { id: 's1', name: 'S', inputs: [], rules: { entries: [], exits: [] } },
    }];
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs });
    expect('data_source' in body.legs.Sig.signal_spec).toBe(false);
  });

  it('an all-v1 portfolio body emits ZERO data_source keys anywhere (byte-identity)', () => {
    const legs = [
      { id: 1, label: 'A', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50 },
      { id: 2, label: 'B', type: 'continuous', collection: 'FUT_SP_500', strategy: 'front_month', adjustment: 'none', weight: 50, dataSource: 'v1' },
    ];
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs });
    expect(JSON.stringify(body).includes('data_source')).toBe(false);
  });
});
