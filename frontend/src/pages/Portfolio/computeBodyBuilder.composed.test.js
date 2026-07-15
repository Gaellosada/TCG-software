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
import { persistedDocToLegs } from './persistedDoc';

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

  it('fund-of-funds: inlines the child OWN range into portfolio.start/end when resolveChildRange is provided', () => {
    const resolvePortfolio = (id) => (id === 'child-1' ? childDoc() : null);
    const resolveChildRange = (id) => (
      id === 'child-1' ? { start: '2005-01-03', end: '2024-06-28' } : null
    );
    const { body } = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio,
      resolveChildRange,
    });
    const child = body.legs.BuildingBlock.portfolio;
    // The child's OWN range is inlined (NOT the parent's) — key-parity with a
    // standalone compute of that child over its overlapRange.
    expect(child.start).toBe('2005-01-03');
    expect(child.end).toBe('2024-06-28');
    expect(child.legs).toHaveProperty('SPX');
  });

  it('fund-of-funds: no start/end when the child range is unresolved (backend computes full overlap)', () => {
    const resolvePortfolio = (id) => (id === 'child-1' ? childDoc() : null);
    const { body } = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio,
      resolveChildRange: () => null, // unresolved
    });
    const child = body.legs.BuildingBlock.portfolio;
    expect(child).not.toHaveProperty('start');
    expect(child).not.toHaveProperty('end');
  });

  it('SC2 parity: the composed-inlined child body deep-equals a STANDALONE top-level build of that child over the same range', () => {
    // The real SC2 invariant: the standalone construction path (a child built
    // at top level, exactly what a standalone /compute sends) and the
    // composed-inlined construction path must produce a field-for-field
    // identical child body. Both must resolve to the SAME backend cache key or
    // composed children would never reuse the standalone cache entry (the
    // original slow-recompute bug). Building both sides here (not from one dict)
    // is what makes this non-tautological.
    const R = { start: '2005-01-03', end: '2024-06-28' };
    const doc = childDoc();

    // STANDALONE: build the child at top level over range R.
    const standalone = buildPortfolioComputeBody({
      legs: persistedDocToLegs(doc),
      rebalance: doc.rebalance,
      start: R.start,
      end: R.end,
      availableIndicators: [],
    });

    // COMPOSED: reference the same child; resolveChildRange returns R.
    const composed = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio: (id) => (id === 'child-1' ? doc : null),
      resolveChildRange: (id) => (id === 'child-1' ? R : null),
    });

    expect(composed.body.legs.BuildingBlock.portfolio).toEqual(standalone.body);
    // Explicit: the inlined child carries {legs, weights, rebalance, return_type,
    // start, end} — the standalone shape verbatim.
    expect(standalone.body).toEqual({
      legs: { SPX: { type: 'instrument', collection: 'INDEX', symbol: 'SPX' } },
      weights: { SPX: 100 },
      rebalance: 'monthly',
      return_type: 'normal',
      start: R.start,
      end: R.end,
    });
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

describe('buildPortfolioComputeBody — global slippage/fees (bps)', () => {
  const pureLegs = [
    { id: 1, label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 },
  ];

  it('omits cost fields when unset / zero (byte-identical body)', () => {
    const { body } = buildPortfolioComputeBody({ ...baseArgs, legs: pureLegs });
    expect('slippage_bps' in body).toBe(false);
    expect('fees_bps' in body).toBe(false);

    const { body: zero } = buildPortfolioComputeBody({
      ...baseArgs, legs: pureLegs, slippageBps: 0, feesBps: 0,
    });
    expect('slippage_bps' in zero).toBe(false);
    expect('fees_bps' in zero).toBe(false);
  });

  it('adds cost fields to the TOP-LEVEL body in bps when > 0', () => {
    const { body } = buildPortfolioComputeBody({
      ...baseArgs, legs: pureLegs, slippageBps: 5, feesBps: 2,
    });
    expect(body.slippage_bps).toBe(5);
    expect(body.fees_bps).toBe(2);
  });

  it('propagates the global cost fields into an inlined child portfolio when > 0', () => {
    // Round-2 fix: a composed child must be computed WITH the same global costs a
    // standalone compute of that child would apply, so the child's OWN internal
    // rebalance/roll trades are charged (previously they were computed cost-free —
    // only the parent allocation layer was charged).
    const resolvePortfolio = (id) => (id === 'child-1' ? childDoc() : null);
    const { body } = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio,
      slippageBps: 5,
      feesBps: 2,
    });
    // Top level carries the costs...
    expect(body.slippage_bps).toBe(5);
    expect(body.fees_bps).toBe(2);
    // ...AND the inlined child sub-body carries the SAME costs (charged inside
    // its own compute; parity with a standalone child body → shared cache key).
    const child = body.legs.BuildingBlock.portfolio;
    expect(child.slippage_bps).toBe(5);
    expect(child.fees_bps).toBe(2);
  });

  it('omits child cost fields when costs are 0 (byte-identical composed body)', () => {
    // Invariant 1: costs-off keeps the child sub-body exactly its pre-feature
    // shape (the 4 §4 keys, no cost keys) at every depth.
    const resolvePortfolio = (id) => (id === 'child-1' ? childDoc() : null);
    const { body } = buildPortfolioComputeBody({
      ...baseArgs, legs: composedLegs, resolvePortfolio, slippageBps: 0, feesBps: 0,
    });
    const child = body.legs.BuildingBlock.portfolio;
    expect('slippage_bps' in child).toBe(false);
    expect('fees_bps' in child).toBe(false);
    expect(Object.keys(child).sort()).toEqual(['legs', 'rebalance', 'return_type', 'weights']);
  });

  it('SC2 key-parity WITH costs on: inlined child == standalone child body', () => {
    // The round-2 fix RESTORES key parity when costs > 0: an inlined child and a
    // standalone compute of the same child (both carrying the same global costs)
    // must build a field-for-field identical child body → same backend cache key.
    // Before the fix the standalone body carried costs and the inlined one did
    // not, so the two keyed differently (parity broken with costs on).
    const R = { start: '2005-01-03', end: '2024-06-28' };
    const doc = childDoc();
    const costs = { slippageBps: 5, feesBps: 2 };

    const standalone = buildPortfolioComputeBody({
      legs: persistedDocToLegs(doc),
      rebalance: doc.rebalance,
      start: R.start,
      end: R.end,
      availableIndicators: [],
      ...costs,
    });
    const composed = buildPortfolioComputeBody({
      ...baseArgs,
      legs: composedLegs,
      resolvePortfolio: (id) => (id === 'child-1' ? doc : null),
      resolveChildRange: (id) => (id === 'child-1' ? R : null),
      ...costs,
    });

    expect(composed.body.legs.BuildingBlock.portfolio).toEqual(standalone.body);
    expect(standalone.body.slippage_bps).toBe(5);
    expect(standalone.body.fees_bps).toBe(2);
  });
});
