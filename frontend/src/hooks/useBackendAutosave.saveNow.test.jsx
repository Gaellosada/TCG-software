// @vitest-environment jsdom
//
// Regression suite for the manual-save + navigation-flush fixes.
//
//   BUG 1 — manual Save must persist immediately and unconditionally,
//           even when autosave is OFF (enabled:false) and no debounce
//           timer is pending.
//   BUG 2 — a pending dirty edit must be flushed (persisted) on
//           unmount / SPA navigation, and an in-flight save that
//           represents unsaved data must NOT be aborted on unmount.
//   (reset() must still abort — that is a context switch, not data loss.)

import React, { useState } from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';
import useBackendAutosave from './useBackendAutosave';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function Harness({ enabled, initialPayload, onSave, debounceMs }) {
  const [payload, setPayload] = useState(initialPayload);
  const { status, saveNow, reset, flush } = useBackendAutosave({
    enabled, payload, onSave, debounceMs,
  });
  Harness.setPayload = setPayload;
  Harness.saveNow = saveNow;
  Harness.reset = reset;
  Harness.flush = flush;
  Harness.lastStatus = status;
  return React.createElement('div', { 'data-testid': 'status' }, status);
}

function mount(props) {
  return render(React.createElement(Harness, props));
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe('useBackendAutosave — saveNow (manual save, BUG 1)', () => {
  it('saveNow() persists immediately when autosave is OFF and no timer is pending', async () => {
    const onSave = vi.fn(() => Promise.resolve());
    // enabled:false === autosave off — the hook never schedules a timer.
    mount({ enabled: false, initialPayload: { v: 1 }, onSave, debounceMs: 3000 });

    expect(onSave).not.toHaveBeenCalled();

    await act(async () => { await Harness.saveNow(); });

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).toEqual({ v: 1 });
    // Threads an abort signal per the onSave contract.
    expect('signal' in onSave.mock.calls[0][1]).toBe(true);
    expect(Harness.lastStatus).toBe('saved');
  });

  it('saveNow() returns a promise that resolves after the save settles', async () => {
    const d = deferred();
    const onSave = vi.fn(() => d.promise);
    mount({ enabled: false, initialPayload: { v: 1 }, onSave, debounceMs: 3000 });

    let settled = false;
    await act(async () => {
      const p = Harness.saveNow().then(() => { settled = true; });
      expect(settled).toBe(false); // still in flight
      d.resolve();
      await p;
    });
    expect(settled).toBe(true);
    expect(Harness.lastStatus).toBe('saved');
  });

  it('saveNow() cancels a pending debounce timer and fires exactly once', async () => {
    vi.useFakeTimers();
    const onSave = vi.fn(() => Promise.resolve());
    mount({ enabled: true, initialPayload: { v: 1 }, onSave, debounceMs: 100 });

    // An edit arms the debounce.
    await act(async () => { Harness.setPayload({ v: 2 }); });
    // Manual save BEFORE the debounce fires.
    await act(async () => { await Harness.saveNow(); });
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).toEqual({ v: 2 });

    // The cancelled timer must not fire a second save.
    await act(async () => { vi.advanceTimersByTime(200); });
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('saveNow(override) persists the explicit payload (rename race guard)', async () => {
    const onSave = vi.fn(() => Promise.resolve());
    mount({ enabled: false, initialPayload: { name: 'old' }, onSave, debounceMs: 3000 });

    await act(async () => { await Harness.saveNow({ name: 'new' }); });
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).toEqual({ name: 'new' });
  });

  it('saveNow(override) while in-flight resolves only AFTER the coalesced restart persists the override (FE-SAVE-2)', async () => {
    // FE-SAVE-2: when saveNow coalesces onto an in-flight save it returned the
    // PRIOR save's promise, which settles when the OLD save finishes — NOT when
    // the restart carrying the override payload completes. Contract (JSDoc): the
    // returned promise settles when THIS save completes. Assert the awaiter is
    // signalled only after the restart's onSave (with the override) settles.
    const deferreds = [];
    const onSave = vi.fn(() => { const d = deferred(); deferreds.push(d); return d.promise; });
    mount({ enabled: false, initialPayload: { v: 1 }, onSave, debounceMs: 3000 });

    // Save #1 in flight (explicit saveNow, no override).
    let firstPromise;
    await act(async () => { firstPromise = Harness.saveNow(); });
    expect(deferreds).toHaveLength(1);

    // saveNow(override) WHILE #1 is in flight → coalesces, returns a promise
    // that must NOT settle until the override is durably persisted.
    let settled = false;
    let overridePromise;
    await act(async () => {
      overridePromise = Harness.saveNow({ v: 99 }).then(() => { settled = true; });
    });
    // Still coalesced — no second concurrent onSave, awaiter unsettled.
    expect(deferreds).toHaveLength(1);
    expect(settled).toBe(false);

    // #1 completes → the queued restart fires with the OVERRIDE payload.
    await act(async () => { deferreds[0].resolve(); await Promise.resolve(); });
    expect(deferreds).toHaveLength(2);
    expect(onSave.mock.calls[1][0]).toEqual({ v: 99 });
    // BUG: the old code resolved here (on #1's completion). The override save
    // (#2) is still in flight — the awaiter must remain unsettled.
    await act(async () => { await Promise.resolve(); });
    expect(settled).toBe(false);

    // #2 (the override) completes → NOW the awaiter resolves.
    await act(async () => { deferreds[1].resolve(); await overridePromise; });
    expect(settled).toBe(true);
    expect(Harness.lastStatus).toBe('saved');
  });

  it('saveNow() coalesces with an in-flight save (no duplicate concurrent onSave)', async () => {
    vi.useFakeTimers();
    const deferreds = [];
    const onSave = vi.fn(() => { const d = deferred(); deferreds.push(d); return d.promise; });
    mount({ enabled: true, initialPayload: { v: 1 }, onSave, debounceMs: 50 });

    // Fire save #1 via the debounce.
    await act(async () => { vi.advanceTimersByTime(50); });
    expect(deferreds).toHaveLength(1);

    // Manual save while #1 is in flight — must coalesce, not double-fire.
    await act(async () => { Harness.setPayload({ v: 2 }); Harness.saveNow(); });
    expect(deferreds).toHaveLength(1);

    // #1 settles → the queued restart fires with the latest payload.
    await act(async () => { deferreds[0].resolve(); await Promise.resolve(); });
    expect(deferreds).toHaveLength(2);
    expect(onSave.mock.calls[1][0]).toEqual({ v: 2 });
  });
});

describe('useBackendAutosave — navigation / unmount flush (BUG 2)', () => {
  it('flushes a pending debounced save on unmount (SPA navigation)', async () => {
    vi.useFakeTimers();
    const onSave = vi.fn(() => Promise.resolve());
    const { unmount } = mount({
      enabled: true, initialPayload: { v: 1 }, onSave, debounceMs: 3000,
    });

    // Edit → debounce armed but NOT yet fired.
    await act(async () => { Harness.setPayload({ v: 2 }); });
    expect(onSave).not.toHaveBeenCalled();

    // Navigate away before the debounce fires.
    await act(async () => { unmount(); });

    // The pending edit must have been persisted, not dropped.
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).toEqual({ v: 2 });
  });

  it('does NOT abort an in-flight save on unmount (in-flight data must complete)', async () => {
    vi.useFakeTimers();
    let capturedSignal = null;
    const d = deferred();
    const onSave = vi.fn((_p, { signal }) => { capturedSignal = signal; return d.promise; });
    const { unmount } = mount({
      enabled: true, initialPayload: { v: 1 }, onSave, debounceMs: 50,
    });

    // Fire the save → in flight.
    await act(async () => { vi.advanceTimersByTime(50); });
    expect(onSave).toHaveBeenCalledTimes(1);

    // Unmount while in flight — must NOT abort (data represents unsaved work).
    await act(async () => { unmount(); });
    expect(capturedSignal.aborted).toBe(false);

    // Let it complete without a React setState-after-unmount warning.
    await act(async () => { d.resolve(); await Promise.resolve(); });
  });

  it('flushes the latest edit on unmount even while a save is already in flight', async () => {
    vi.useFakeTimers();
    const deferreds = [];
    const onSave = vi.fn(() => { const d = deferred(); deferreds.push(d); return d.promise; });
    const { unmount } = mount({
      enabled: true, initialPayload: { v: 1 }, onSave, debounceMs: 50,
    });

    // Save #1 in flight.
    await act(async () => { vi.advanceTimersByTime(50); });
    expect(deferreds).toHaveLength(1);

    // A newer edit arms a fresh timer, THEN the user navigates away.
    await act(async () => { Harness.setPayload({ v: 2 }); });
    await act(async () => { unmount(); });

    // #1 settles → the queued latest edit must still be persisted.
    await act(async () => { deferreds[0].resolve(); await Promise.resolve(); });
    expect(deferreds).toHaveLength(2);
    expect(onSave.mock.calls[1][0]).toEqual({ v: 2 });
  });

  it('save-status recovers after a StrictMode remount (mountedRef re-arm regression)', async () => {
    // TEST-REMOUNT: React StrictMode mounts→unmounts→remounts on the SAME
    // fiber, so the persistent ``mountedRef`` is set false by the first
    // cleanup. Without re-arming it to true on (re)mount, every subsequent
    // setStatus is silently skipped and the save-status indicator never
    // appears even though the save fires. Assert status recovers post-remount.
    const d = deferred();
    const onSave = vi.fn(() => d.promise);
    render(React.createElement(
      React.StrictMode,
      null,
      React.createElement(Harness, { enabled: false, initialPayload: { v: 1 }, onSave }),
    ));

    // Trigger a save AFTER the StrictMode mount→unmount→remount probe.
    let p;
    await act(async () => { p = Harness.saveNow(); });
    // If mountedRef was left false by the remount, setStatus('saving') is a
    // no-op and this stays 'idle'.
    expect(Harness.lastStatus).toBe('saving');

    await act(async () => { d.resolve(); await p; });
    expect(Harness.lastStatus).toBe('saved');
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('reset() still aborts the in-flight save (selection switch is not data loss)', async () => {
    vi.useFakeTimers();
    let capturedSignal = null;
    const d = deferred();
    const onSave = vi.fn((_p, { signal }) => { capturedSignal = signal; return d.promise; });
    mount({ enabled: true, initialPayload: { v: 1 }, onSave, debounceMs: 50 });

    await act(async () => { vi.advanceTimersByTime(50); });
    expect(onSave).toHaveBeenCalledTimes(1);

    await act(async () => { Harness.reset(); });
    expect(capturedSignal.aborted).toBe(true);
  });
});
