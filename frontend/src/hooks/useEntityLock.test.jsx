// @vitest-environment jsdom
//
// Unit tests for the shared useEntityLock hook used by the Signals,
// Indicators and Portfolio pages. Verifies the two flavours that the three
// pages use: server-confirmed (non-optimistic) and optimistic + rollback.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';
import useEntityLock from './useEntityLock';

afterEach(() => { cleanup(); });

describe('useEntityLock — server-confirmed (non-optimistic)', () => {
  it('calls onStart, awaits setLocked, applies the server locked, then onSuccess', async () => {
    const calls = [];
    const setLocked = vi.fn(async () => ({ locked: true }));
    const applyLocked = vi.fn((id, v) => calls.push(['apply', id, v]));
    const onStart = vi.fn(() => calls.push(['start']));
    const onSuccess = vi.fn((doc) => calls.push(['success', doc.locked]));
    const onError = vi.fn();

    const { result } = renderHook(() =>
      useEntityLock({ setLocked, applyLocked, onStart, onSuccess, onError }),
    );
    await act(async () => { await result.current('id-1', true); });

    expect(setLocked).toHaveBeenCalledWith('id-1', true);
    // No optimistic flip: applyLocked is called exactly once, on success.
    expect(applyLocked).toHaveBeenCalledTimes(1);
    expect(applyLocked).toHaveBeenCalledWith('id-1', true, { locked: true });
    expect(onError).not.toHaveBeenCalled();
    expect(calls).toEqual([['start'], ['apply', 'id-1', true], ['success', true]]);
  });

  it('falls back to the requested value when the server omits locked', async () => {
    const setLocked = vi.fn(async () => ({}));
    const applyLocked = vi.fn();
    const { result } = renderHook(() =>
      useEntityLock({ setLocked, applyLocked }),
    );
    await act(async () => { await result.current('id-1', false); });
    expect(applyLocked).toHaveBeenCalledWith('id-1', false, {});
  });

  it('on failure calls onError and does NOT roll back (non-optimistic)', async () => {
    const err = new Error('boom');
    const setLocked = vi.fn(async () => { throw err; });
    const applyLocked = vi.fn();
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useEntityLock({ setLocked, applyLocked, onError }),
    );
    await act(async () => { await result.current('id-1', true); });
    // applyLocked never runs (no optimistic flip, no success).
    expect(applyLocked).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(err);
  });
});

describe('useEntityLock — optimistic + rollback', () => {
  it('flips optimistically before the request, then applies the server value', async () => {
    const calls = [];
    const setLocked = vi.fn(async () => ({ locked: true }));
    const applyLocked = vi.fn((id, v) => calls.push(v));
    const { result } = renderHook(() =>
      useEntityLock({ setLocked, applyLocked, optimistic: true }),
    );
    await act(async () => { await result.current('id-1', true); });
    // First the optimistic flip (true), then the server-confirmed value (true).
    expect(applyLocked).toHaveBeenCalledTimes(2);
    expect(calls).toEqual([true, true]);
  });

  it('rolls back to !next on failure', async () => {
    const calls = [];
    const setLocked = vi.fn(async () => { throw new Error('nope'); });
    const applyLocked = vi.fn((id, v) => calls.push(v));
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useEntityLock({ setLocked, applyLocked, optimistic: true, onError }),
    );
    await act(async () => { await result.current('id-1', true); });
    // Optimistic flip to true, then rollback to false.
    expect(calls).toEqual([true, false]);
    expect(onError).toHaveBeenCalled();
  });
});
