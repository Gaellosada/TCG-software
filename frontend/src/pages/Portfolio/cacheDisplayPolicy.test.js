// @vitest-environment node
//
// Unit coverage for FIX A's pure decision: whether an in-flight compute's
// result should be DISPLAYED when it lands, given the live config may have
// changed mid-flight.

import { describe, it, expect } from 'vitest';
import { shouldDisplayComputeResult } from './cacheDisplayPolicy';

describe('shouldDisplayComputeResult()', () => {
  it('cache OFF → always display (today’s behavior), regardless of keys', () => {
    expect(shouldDisplayComputeResult({ cacheOn: false, computeKey: 'a', liveKey: 'b' })).toBe(true);
    expect(shouldDisplayComputeResult({ cacheOn: false, computeKey: null, liveKey: null })).toBe(true);
  });

  it('cache ON, live config unchanged (keys equal) → display', () => {
    expect(shouldDisplayComputeResult({ cacheOn: true, computeKey: 'k1', liveKey: 'k1' })).toBe(true);
  });

  it('cache ON, edited mid-compute (keys differ) → DROP (stay blank)', () => {
    expect(shouldDisplayComputeResult({ cacheOn: true, computeKey: 'keyA', liveKey: 'keyB' })).toBe(false);
  });

  it('cache ON, live key went null (config gated after edit) → DROP', () => {
    expect(shouldDisplayComputeResult({ cacheOn: true, computeKey: 'keyA', liveKey: null })).toBe(false);
  });

  it('cache ON but computeKey null (hash unavailable) → display (best-effort, never suppress)', () => {
    expect(shouldDisplayComputeResult({ cacheOn: true, computeKey: null, liveKey: 'anything' })).toBe(true);
  });
});
