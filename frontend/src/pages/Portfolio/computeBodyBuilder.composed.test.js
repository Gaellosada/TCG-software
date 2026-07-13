// Composed-portfolio builder tests (design §4 wire contract + live-ref
// invalidation A1-3 / A1-4).
//
// buildPortfolioComputeBody must, for a ``type:"portfolio"`` leg:
//   - resolve the child's CURRENT saved doc via the injected resolver,
//   - inline it under ``portfolio`` = {legs, weights, rebalance, return_type}
//     (NO start/end) with a provenance ``portfolio_id`` (design §4),
//   - so editing the child changes the body AND the content-addressed cache key
//     (the free-invalidation mechanism), and
//   - report an unresolved child as a broken reference (not emit the leg).

import { describe, it, expect } from 'vitest';
import { buildPortfolioComputeBody } from './computeBodyBuilder';

// A saved PURE child portfolio (one instrument leg). ``resolvePortfolio``
// returns this by id; editing it (below) changes the inlined spec.
function childDoc(overrides = {}) {
  return {
    id: 'child-1',
    name: 'SPX 10-delta put',
    category: 'RESEARCH',
    kind: 'pure',
    rebalance: 'monthly',
    legs: [
      { label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 },
    ],
    ...overrides,
  };
}

// A composed leg referencing that child.
const composedLegs = [
  { id: 1, label: 'BuildingBlock', type: 'portfolio', portfolioId: 'child-1', weight: 60 },
];

const baseArgs = {
  rebalance: 'none',
  start: '2020-01-01',
  end: '2021-01-01',
  availableIndicators: [],
};

describe('buildPortfolioComputeBody — composed (portfolio) legs', () => {
  it('inlines the resolved child under `portfolio` matching design §4', () => {
    const resolvePortfolio = (id) => (id === 'child-1' ? childDoc() : null);
    const { body, brokenRefs } = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio,
    });

    expect(brokenRefs).toEqual([]);
    const leg = body.legs.BuildingBlock;
    expect(leg.type).toBe('portfolio');
    expect(leg.portfolio_id).toBe('child-1');           // provenance only
    // The nested portfolio is a resolved sub-body — NO start/end.
    expect(leg.portfolio).toEqual({
      legs: { SPX: { type: 'instrument', collection: 'INDEX', symbol: 'SPX' } },
      weights: { SPX: 100 },
      rebalance: 'monthly',
      return_type: 'normal',
    });
    expect(leg.portfolio).not.toHaveProperty('start');
    expect(leg.portfolio).not.toHaveProperty('end');
    // Parent weight applied at the top level as with any leg.
    expect(body.weights.BuildingBlock).toBe(60);
  });

  it('live reference: editing the child spec changes the inlined compute body', () => {
    const before = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio: (id) => (id === 'child-1' ? childDoc() : null),
    });
    // Child edited: weight 100 → 50 (a live reference — resolver returns the new
    // current spec; the leg is NOT re-added by the user).
    const after = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio: (id) => (id === 'child-1'
        ? childDoc({ legs: [{ label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50 }] })
        : null),
    });

    // The inlined child spec changed → the content-addressed body changed, so
    // the backend's key changes → recompute (live-ref invalidation).
    expect(after.body).not.toEqual(before.body);
    expect(after.body.legs.BuildingBlock.portfolio.weights.SPX).toBe(50);
  });

  it('reports an unresolved child as a broken reference and emits no leg', () => {
    const { body, brokenRefs } = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio: () => null,   // child deleted/archived/empty
    });
    expect(brokenRefs).toEqual([
      { label: 'BuildingBlock', portfolioId: 'child-1', reason: 'unresolved' },
    ]);
    expect(body.legs).not.toHaveProperty('BuildingBlock');
    // No dangling weight for an unemitted leg.
    expect(body.weights).not.toHaveProperty('BuildingBlock');
  });

  it('depth-1: a portfolio leg INSIDE a child is a broken ref (never inlined)', () => {
    // A malicious/stale child that itself references a portfolio. The picker
    // prevents this, but the builder must terminate recursion regardless.
    const nastyChild = childDoc({
      legs: [{ label: 'Inner', type: 'portfolio', portfolioId: 'child-1', weight: 100 }],
    });
    const { body, brokenRefs } = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio: () => nastyChild,
    });
    // The parent leg's child had a depth-exceeded ref → the parent leg is not
    // emitted (its inlined child would be empty) and the broken ref surfaces.
    expect(brokenRefs.length).toBeGreaterThanOrEqual(1);
    expect(brokenRefs.some((b) => b.reason === 'depth')).toBe(true);
  });

  it('two composed legs combine, weights preserved', () => {
    const resolvePortfolio = (id) => {
      if (id === 'a') return childDoc({ id: 'a', legs: [{ label: 'X', type: 'instrument', collection: 'C', symbol: 'X', weight: 100 }] });
      if (id === 'b') return childDoc({ id: 'b', legs: [{ label: 'Y', type: 'instrument', collection: 'C', symbol: 'Y', weight: 100 }] });
      return null;
    };
    const { body, brokenRefs } = buildPortfolioComputeBody({
      ...baseArgs,
      legs: [
        { id: 1, label: 'A', type: 'portfolio', portfolioId: 'a', weight: 40 },
        { id: 2, label: 'B', type: 'portfolio', portfolioId: 'b', weight: 60 },
      ],
      resolvePortfolio,
    });
    expect(brokenRefs).toEqual([]);
    expect(body.weights).toEqual({ A: 40, B: 60 });
    expect(body.legs.A.portfolio.legs).toHaveProperty('X');
    expect(body.legs.B.portfolio.legs).toHaveProperty('Y');
  });

  it('pure legs are byte-identical whether or not a resolver is passed', () => {
    const pureLegs = [
      { id: 1, label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 },
    ];
    const withResolver = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs, resolvePortfolio: () => null });
    const without = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs });
    expect(withResolver.body).toEqual(without.body);
    expect(without.brokenRefs).toEqual([]);
  });
});
