// Per-instrument data_source persistence round-trip (portfolio localStorage).
//
// Invariants:
//   1. A v2 leg's source survives save → load; a v1 leg carries no key.
//   2. An all-v1 portfolio round-trips with NO ``dataSource`` key anywhere —
//      byte-stable, so a pre-feature saved doc reloads without a spurious diff.

import { describe, it, expect, beforeEach } from 'vitest';
import { savePortfolio, loadPortfolio } from './storage';
import { persistedDocToLegs } from './persistedDoc';

beforeEach(() => {
  localStorage.clear();
});

const legV2 = { label: 'A', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50, dataSource: 'v2' };
const legV1 = { label: 'B', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 50, dataSource: 'v1' };
const legBare = { label: 'C', type: 'instrument', collection: 'INDEX', symbol: 'SPX', weight: 100 };

describe('portfolio persistence — per-instrument data_source', () => {
  it('preserves a v2 leg source and omits the key on a v1 leg after save → load', () => {
    savePortfolio('mix', { legs: [legV2, legV1], rebalance: 'none' });
    const doc = loadPortfolio('mix');
    const [a, b] = doc.legs;
    expect(a.dataSource).toBe('v2');
    expect('dataSource' in b).toBe(false); // v1 omitted on write

    // …and the doc→legs converter carries the source back for the builder.
    const legs = persistedDocToLegs(doc);
    expect(legs[0].dataSource).toBe('v2');
    expect('dataSource' in legs[1]).toBe(false);
  });

  it('an all-v1 portfolio persists NO dataSource key anywhere (byte-stable)', () => {
    savePortfolio('purev1', { legs: [legV1, legBare], rebalance: 'none' });
    const doc = loadPortfolio('purev1');
    for (const leg of doc.legs) {
      expect('dataSource' in leg).toBe(false);
    }
  });
});
