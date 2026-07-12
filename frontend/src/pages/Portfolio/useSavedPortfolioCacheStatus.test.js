// @vitest-environment node
//
// Unit test for the pure row-status derivation.

import { describe, it, expect } from 'vitest';
import { statusForKey } from './useSavedPortfolioCacheStatus';

describe('statusForKey()', () => {
  it('no key (unresolvable body/range) → not-cached', () => {
    expect(statusForKey(null, false)).toBe('not-cached');
    expect(statusForKey(null, true)).toBe('not-cached');
    expect(statusForKey(undefined, undefined)).toBe('not-cached');
  });

  it('key present in cache → cached', () => {
    expect(statusForKey('abc', true)).toBe('cached');
  });

  it('key absent from cache → not-cached', () => {
    expect(statusForKey('abc', false)).toBe('not-cached');
  });
});
