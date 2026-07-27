// data_source wire-field tests for the portfolio compute body.
//
// Invariants under test:
//   1. v1 (and undefined) emit NO ``data_source`` key at all — a default body is
//      byte-identical to a pre-feature payload (the backend hashes the whole
//      body for its result cache; an unconditional "v1" would invalidate every
//      existing entry).
//   2. v2 emits ``data_source: "v2"`` on the TOP-LEVEL body.
//   3. v2 also emits it on every INLINED COMPOSED CHILD sub-body — otherwise a
//      composed v2 portfolio silently computes its children against v1.

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

describe('buildPortfolioComputeBody — data_source', () => {
  it('omits data_source entirely when the source is v1', () => {
    const { body } = buildPortfolioComputeBody({
      ...baseArgs, legs: pureLegs, dataSource: 'v1',
    });
    expect('data_source' in body).toBe(false);
  });

  it('omits data_source when the source is not supplied at all', () => {
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs });
    expect('data_source' in body).toBe(false);
  });

  it('a v1 body is byte-identical to a no-source body', () => {
    const a = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs }).body;
    const b = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v1' }).body;
    expect(JSON.stringify(b)).toBe(JSON.stringify(a));
  });

  it('emits data_source:"v2" on the top-level body when v2 is selected', () => {
    const { body } = buildPortfolioComputeBody({
      ...baseArgs, legs: pureLegs, dataSource: 'v2',
    });
    expect(body.data_source).toBe('v2');
  });

  it('propagates data_source into the INLINED COMPOSED CHILD sub-body', () => {
    const { body, brokenRefs } = buildPortfolioComputeBody({
      ...baseArgs, legs: composedLegs, resolvePortfolio, dataSource: 'v2',
    });
    expect(brokenRefs).toEqual([]);
    expect(body.data_source).toBe('v2');
    // The child must carry it too — a composed v2 portfolio whose children
    // resolve against v1 is the silently-wrong case.
    expect(body.legs.BuildingBlock.portfolio.data_source).toBe('v2');
  });

  it('a v1 composed child carries no data_source key', () => {
    const { body } = buildPortfolioComputeBody({
      ...baseArgs, legs: composedLegs, resolvePortfolio, dataSource: 'v1',
    });
    expect('data_source' in body).toBe(false);
    expect('data_source' in body.legs.BuildingBlock.portfolio).toBe(false);
  });

  it('a signal leg sub-body carries NO data_source (top-level field only, as costs)', () => {
    const legs = [{
      id: 1,
      label: 'Sig',
      type: 'signal',
      weight: 100,
      signalSpec: { id: 's1', name: 'S', inputs: [], rules: { entries: [], exits: [] } },
    }];
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs, dataSource: 'v2' });
    expect(body.data_source).toBe('v2');
    expect('data_source' in body.legs.Sig.signal_spec).toBe(false);
  });

  it('changing the source changes the serialized body — the backend cache key differs', () => {
    const v1 = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v1' }).body;
    const v2 = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, dataSource: 'v2' }).body;
    expect(JSON.stringify(v1)).not.toBe(JSON.stringify(v2));
  });
});
