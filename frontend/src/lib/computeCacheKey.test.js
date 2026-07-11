// @vitest-environment jsdom
//
// Unit tests for the content-addressed cache key: determinism, key-order
// invariance, undefined-dropping, and a KNOWN VECTOR cross-checked against an
// independent SHA-256 implementation (node:crypto vs the WebCrypto path).

import { describe, it, expect } from 'vitest';
import { createHash } from 'node:crypto';
import { canonicalize, computeCacheKey } from './computeCacheKey';

// Independent oracle: hash a string with node's crypto (not crypto.subtle).
function nodeSha256Hex(str) {
  return createHash('sha256').update(str, 'utf8').digest('hex');
}

describe('canonicalize()', () => {
  it('sorts object keys recursively', () => {
    const out = canonicalize({ b: 1, a: { d: 4, c: 3 } });
    expect(JSON.stringify(out)).toBe('{"a":{"c":3,"d":4},"b":1}');
  });

  it('preserves array order (order is semantically meaningful)', () => {
    const out = canonicalize({ xs: [3, 1, 2] });
    expect(JSON.stringify(out)).toBe('{"xs":[3,1,2]}');
  });

  it('drops undefined-valued keys (matches the wire body / JSON)', () => {
    const out = canonicalize({ a: 1, b: undefined });
    expect(Object.keys(out)).toEqual(['a']);
  });
});

describe('computeCacheKey()', () => {
  it('returns a 64-char lowercase hex string', async () => {
    const key = await computeCacheKey({ a: 1 });
    expect(key).toMatch(/^[0-9a-f]{64}$/);
  });

  it('matches an independent SHA-256 of the canonical JSON (known vector)', async () => {
    const body = { legs: { SPX: { type: 'instrument' } }, weights: { SPX: 100 } };
    const canonicalJson = JSON.stringify(canonicalize(body));
    const expected = nodeSha256Hex(canonicalJson);
    expect(await computeCacheKey(body)).toBe(expected);
  });

  it('is deterministic — same body → same key', async () => {
    const body = { legs: { A: { type: 'instrument', collection: 'X' } }, weights: { A: 50 } };
    const k1 = await computeCacheKey(body);
    const k2 = await computeCacheKey(body);
    expect(k1).toBe(k2);
  });

  it('is key-order invariant — reordered keys → same key', async () => {
    const a = await computeCacheKey({ legs: { A: 1 }, weights: { A: 2 }, rebalance: 'none' });
    const b = await computeCacheKey({ rebalance: 'none', weights: { A: 2 }, legs: { A: 1 } });
    expect(a).toBe(b);
  });

  it('nested key order does not affect the key', async () => {
    const a = await computeCacheKey({ leg: { type: 't', collection: 'c', symbol: 's' } });
    const b = await computeCacheKey({ leg: { symbol: 's', collection: 'c', type: 't' } });
    expect(a).toBe(b);
  });

  it('a real difference changes the key', async () => {
    const a = await computeCacheKey({ start: '2020-01-01', end: '2020-12-31' });
    const b = await computeCacheKey({ start: '2020-01-01', end: '2021-12-31' });
    expect(a).not.toBe(b);
  });

  it('treats a dropped undefined start the same as an omitted start', async () => {
    const a = await computeCacheKey({ legs: {}, start: undefined });
    const b = await computeCacheKey({ legs: {} });
    expect(a).toBe(b);
  });
});
