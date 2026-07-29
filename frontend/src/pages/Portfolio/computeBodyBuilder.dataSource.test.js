// Per-instrument data_source wire-field tests for the portfolio compute body.
//
// Invariants under test:
//   1. v1 (and undefined) emit NO ``data_source`` key anywhere — a default body
//      is byte-identical to a pre-feature payload (the backend hashes the whole
//      body for its result cache; an unconditional "v1" would invalidate every
//      existing entry).
//   2. PER-INSTRUMENT: the source rides each LEAF (its own ``dataSource`` → the
//      build default fold → v1), emitted only for 'v2'. There is NO top-level
//      ``data_source`` body field — the page "set all" control is a seed folded
//      per leaf, not a separate wire concept.
//   3. A composed child keeps a body-level default AND folds it onto its leaves.

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
      { label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 },
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
    const v1 = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v1' }).body;
    const v2 = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v2' }).body;
    expect('data_source' in v1).toBe(false);
    expect('data_source' in v2).toBe(false);
  });

  it('omits data_source on a v1 leaf and on a no-source build', () => {
    const a = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v1' }).body;
    const b = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs }).body;
    expect('data_source' in a.legs.SPX).toBe(false);
    expect('data_source' in b.legs.SPX).toBe(false);
  });

  it('a v1 body is byte-identical to a no-source body', () => {
    const a = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs }).body;
    const b = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v1' }).body;
    expect(JSON.stringify(b)).toBe(JSON.stringify(a));
  });

  it('folds the build default onto an unset leaf as its per-leaf source', () => {
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v2' });
    expect(body.legs.SPX.data_source).toBe('v2');
  });

  // The load-bearing per-instrument case: one flat portfolio, a v1 leg + a v2 leg.
  it('emits data_source:"v2" on the v2 leg and NO key on the v1 leg', () => {
    const legs = [
      { id: 1, label: 'A', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50, dataSource: 'v1' },
      { id: 2, label: 'B', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50, dataSource: 'v2' },
    ];
    // No build default → the v1 leg's own source wins (no fold to v2).
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs });
    expect('data_source' in body.legs.A).toBe(false);
    expect(body.legs.B.data_source).toBe('v2');
    expect('data_source' in body).toBe(false); // still no top-level field
  });

  it('an explicit v1 leaf overrides a v2 build default (emits no key)', () => {
    const legs = [
      { id: 1, label: 'A', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50, dataSource: 'v1' },
      { id: 2, label: 'B', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50 },
    ];
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs, dataSource: 'v2' });
    expect('data_source' in body.legs.A).toBe(false); // v1 override → omitted
    expect(body.legs.B.data_source).toBe('v2');       // unset → folds the v2 default
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

  it('propagates the source onto the INLINED COMPOSED CHILD (body-level + leaf)', () => {
    const { body, brokenRefs } = buildPortfolioComputeBody({
      ...baseArgs, legs: composedLegs, resolvePortfolio, dataSource: 'v2',
    });
    expect(brokenRefs).toEqual([]);
    expect('data_source' in body).toBe(false);                        // no top-level
    expect(body.legs.BuildingBlock.portfolio.data_source).toBe('v2'); // child body default
    expect(body.legs.BuildingBlock.portfolio.legs.SPX.data_source).toBe('v2'); // folded leaf
  });

  it('a v1 composed child carries no data_source key (body or leaf)', () => {
    const { body } = buildPortfolioComputeBody({
      ...baseArgs, legs: composedLegs, resolvePortfolio, dataSource: 'v1',
    });
    expect('data_source' in body.legs.BuildingBlock.portfolio).toBe(false);
    expect('data_source' in body.legs.BuildingBlock.portfolio.legs.SPX).toBe(false);
  });

  it("a composed child's own dataSource override wins over the parent default", () => {
    const legs = [{ ...composedLegs[0], dataSource: 'v2' }];
    const { body } = buildPortfolioComputeBody({
      ...baseArgs, legs, resolvePortfolio, dataSource: 'v1',
    });
    expect('data_source' in body).toBe(false);
    expect(body.legs.BuildingBlock.portfolio.data_source).toBe('v2');
    expect(body.legs.BuildingBlock.portfolio.legs.SPX.data_source).toBe('v2');
  });

  it('mixes v1 and v2 composed children in one body', () => {
    const legs = [
      { id: 1, label: 'A', type: 'portfolio', portfolioId: 'child-1', weight: 50, dataSource: 'v1' },
      { id: 2, label: 'B', type: 'portfolio', portfolioId: 'child-1', weight: 50, dataSource: 'v2' },
    ];
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs, resolvePortfolio, dataSource: 'v1' });
    expect('data_source' in body.legs.A.portfolio).toBe(false);
    expect(body.legs.B.portfolio.data_source).toBe('v2');
  });

  it('a signal leg sub-body carries NO top-level data_source (source rides its inputs)', () => {
    const legs = [{
      id: 1,
      label: 'Sig',
      type: 'signal',
      weight: 100,
      signalSpec: { id: 's1', name: 'S', inputs: [], rules: { entries: [], exits: [] } },
    }];
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs, dataSource: 'v2' });
    expect('data_source' in body.legs.Sig.signal_spec).toBe(false);
  });

  it('changing a leaf source changes the serialized body — the backend cache key differs', () => {
    const v1 = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v1' }).body;
    const v2 = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v2' }).body;
    expect(JSON.stringify(v1)).not.toBe(JSON.stringify(v2));
  });

  // "Set all v2" seed behavior: the page control does not add a body field — it
  // seeds each leaf's own source, which the builder then folds/emits per leaf.
  it('seed-all-v2 (build default) makes every unset leaf emit v2, still no top-level field', () => {
    const legs = [
      { id: 1, label: 'A', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50 },
      { id: 2, label: 'B', type: 'continuous', collection: 'FUT_SP_500', strategy: 'front_month', adjustment: 'none', weight: 50 },
    ];
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs, dataSource: 'v2' });
    expect('data_source' in body).toBe(false);
    expect(body.legs.A.data_source).toBe('v2');
    expect(body.legs.B.data_source).toBe('v2');
  });
});
