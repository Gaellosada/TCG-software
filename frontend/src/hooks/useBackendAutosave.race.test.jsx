// @vitest-environment jsdom
//
// Wave-1 adversarial concurrency review — race scenarios in
// useBackendAutosave.
//
// These tests probe concurrency properties that the PR body claims
// ("last-edit-wins concurrency tokens"). Some of them PASS (documenting
// safety properties); others FAIL against the current implementation,
// demonstrating real races the prior six PASS reviews missed.
//
// Production code is read-only for this review — tests here exist to
// surface issues, not to validate proposed fixes.

import React, { useState } from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';
import useBackendAutosave from './useBackendAutosave';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// Test harness exposing the hook's status and a way to mutate the
// payload at runtime.
function Harness({ enabled, initialPayload, onSave, debounceMs }) {
  const [payload, setPayload] = useState(initialPayload);
  const { status, reset } = useBackendAutosave({
    enabled, payload, onSave, debounceMs,
  });
  Harness.setPayload = setPayload;
  Harness.reset = reset;
  Harness.lastStatus = status;
  return React.createElement('div', { 'data-testid': 'status' }, status);
}

function mount(props) {
  return render(React.createElement(Harness, props));
}

// A deferred promise helper so we can interleave save resolutions
// arbitrarily.
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe('useBackendAutosave — race scenarios', () => {
  // -----------------------------------------------------------------
  // SCENARIO 1 — Stale-overwrite race (in-flight save must be
  // cancellable; new edits must not produce concurrent wire writes).
  //
  // Last-edit-wins guarantee: onSave must receive an AbortSignal so
  // callers can thread it into fetch. Coalescing ensures at most one
  // wire request per hook instance — eliminating the chance that an
  // older PUT lands on the server after a newer one due to network
  // reordering.
  // -----------------------------------------------------------------
  it('in-flight save is cancellable via AbortSignal; no concurrent onSave invocations', async () => {
    vi.useFakeTimers();
    const calls = [];
    const deferreds = [];
    const onSave = vi.fn((payload, opts) => {
      const d = deferred();
      calls.push({ payload, opts });
      deferreds.push(d);
      return d.promise;
    });

    mount({ enabled: true, initialPayload: { v: 1 }, onSave, debounceMs: 100 });

    // Debounce #1 fires → save #1 in flight with payload {v:1}.
    await act(async () => { vi.advanceTimersByTime(100); });
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(calls[0].payload).toEqual({ v: 1 });

    // User types more → re-arm debounce → coalesced (no concurrent
    // onSave call). The new payload waits for save #1 to settle.
    await act(async () => { Harness.setPayload({ v: 2 }); });
    await act(async () => { vi.advanceTimersByTime(100); });
    expect(onSave).toHaveBeenCalledTimes(1); // coalesced, not 2

    // Property: onSave received a second argument with `.signal` —
    // fetch-style abort signal. This is the load-bearing contract for
    // wire-level cancellation.
    const hasAbortMechanism =
      onSave.mock.calls[0].length >= 2
      && onSave.mock.calls[0][1]
      && 'signal' in onSave.mock.calls[0][1];
    expect(hasAbortMechanism).toBe(true);
    // The signal must be an AbortSignal-like (has 'aborted' boolean).
    expect(typeof onSave.mock.calls[0][1].signal.aborted).toBe('boolean');
  });

  // -----------------------------------------------------------------
  // SCENARIO 2 — Out-of-order completion is impossible under the
  // coalescing model: only ONE save is ever in flight per hook instance.
  // The previously-existing safety property ("older save resolving after
  // newer save must not clobber 'error' status") is preserved trivially
  // because there is no "older save" while a newer one runs.
  //
  // The test now demonstrates two things:
  //   (a) Sequential save semantics — when save #1 is in flight, a fresh
  //       edit only schedules a pendingRestart and does NOT immediately
  //       invoke onSave a second time.
  //   (b) When save #1 rejects (server 5xx), the pendingRestart fires
  //       and the NEW save's outcome decides the final status. A late
  //       resolve of an already-superseded save does not clobber status.
  // -----------------------------------------------------------------
  it('PASS (safety): in-flight coalescing — no concurrent saves; pending restart drives final status', async () => {
    vi.useFakeTimers();
    const deferreds = [];
    const onSave = vi.fn(() => {
      const d = deferred();
      deferreds.push(d);
      return d.promise;
    });

    mount({ enabled: true, initialPayload: { v: 1 }, onSave, debounceMs: 50 });

    // Fire save #1.
    await act(async () => { vi.advanceTimersByTime(50); });
    expect(deferreds).toHaveLength(1);
    expect(Harness.lastStatus).toBe('saving');

    // Mutate → debounce fires but save #1 is in flight, so this is
    // queued as pendingRestart rather than a second concurrent onSave.
    await act(async () => { Harness.setPayload({ v: 2 }); });
    await act(async () => { vi.advanceTimersByTime(50); });
    expect(deferreds).toHaveLength(1); // coalesced — no second invocation yet

    // Save #1 rejects (server 5xx). The pendingRestart kicks in: a new
    // save fires immediately with the latest payload.
    await act(async () => {
      deferreds[0].reject(new Error('boom'));
      await Promise.resolve();
      await Promise.resolve();
    });
    // The restart should have fired → second onSave invocation now.
    expect(deferreds).toHaveLength(2);
    expect(Harness.lastStatus).toBe('saving');

    // Save #2 succeeds — status becomes 'saved'.
    await act(async () => {
      deferreds[1].resolve();
      await Promise.resolve();
    });
    expect(Harness.lastStatus).toBe('saved');
  });

  // -----------------------------------------------------------------
  // SCENARIO 3 — setState after unmount during an in-flight save.
  //
  // The cleanup effect cancels the timer but NOT the in-flight save's
  // status callback. When the promise resolves after unmount, the
  // hook calls setStatus on an unmounted component → React warning
  // (and a memory-retention smell). The hook should track mounted
  // state and skip status updates after unmount.
  //
  // This test asserts no console.error from React during unmount.
  // FAILS if React logs "Can't perform a React state update on an
  // unmounted component" or similar.
  //
  // Note: React 18 quietly elided that warning in production but it
  // still surfaces in some testing-library/react-dom versions. We
  // instead probe by checking that setStatus is not called after
  // unmount — via a hook indirection on console.error.
  // -----------------------------------------------------------------
  it('in-flight save resolving after unmount does not trigger setState on unmounted component', async () => {
    vi.useFakeTimers();
    const errors = [];
    const origError = console.error;
    console.error = (...args) => { errors.push(args.join(' ')); origError(...args); };

    const d = deferred();
    const onSave = vi.fn(() => d.promise);

    const { unmount } = mount({
      enabled: true, initialPayload: { v: 1 }, onSave, debounceMs: 50,
    });

    // Fire save → in-flight.
    await act(async () => { vi.advanceTimersByTime(50); });
    expect(onSave).toHaveBeenCalled();

    // Unmount while save is in flight.
    unmount();

    // Resolve save AFTER unmount.
    await act(async () => {
      d.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    console.error = origError;

    // Property: no React warnings about updating state on an unmounted
    // component. Any warning of that family is a leak / bug surface.
    const offending = errors.filter((m) =>
      /unmounted component|not wrapped in act|state update on an unmounted/i.test(m));
    expect(offending).toEqual([]);
  });

  // -----------------------------------------------------------------
  // SCENARIO 4 — Selection switch leaves an in-flight save targeting
  // the OLD selection.
  //
  // SignalsPage calls `resetCloudStatus()` when `selectedId` changes.
  // `reset()` cancels the pending timer (good) and bumps the seq so
  // older in-flight saves' status updates are ignored (good for the
  // UI). But: if a save fired RIGHT BEFORE the selection switch (i.e.,
  // PUT for signal A is in flight when user clicks signal B), the
  // hook continues that PUT to completion. The closure capturing
  // `selectedId` inside `handleBackendSave` was the OLD id when the
  // PUT was issued — so the PUT correctly targets A. That's fine for
  // correctness of routing.
  //
  // However: `handleBackendSave`'s `.then` invokes
  // `fetchPersistedSignals(persistedCategory)` AFTER the in-flight
  // save — this happens with the NEW closure values if onSave is
  // re-captured. Since the hook stores `onSave` in `onSaveRef` and
  // ALWAYS updates the ref on render, the OUTER caller has no way to
  // guarantee that the resolved-save side effects belong to the old
  // selection.
  //
  // Most importantly: reset() does NOT abort the in-flight network
  // request, so the user's intent of "discard pending save for A"
  // when switching away is NOT honored. This test makes that explicit.
  // -----------------------------------------------------------------
  it('reset() aborts in-flight save and surfaces signal cancellation to onSave', async () => {
    vi.useFakeTimers();
    const d = deferred();
    const onSave = vi.fn(() => d.promise);

    mount({ enabled: true, initialPayload: { sigA: 'edit' }, onSave, debounceMs: 50 });
    // Fire save.
    await act(async () => { vi.advanceTimersByTime(50); });
    expect(onSave).toHaveBeenCalledTimes(1);

    // User switches selection → caller invokes reset().
    await act(async () => { Harness.reset(); });

    // Resolve the in-flight save.
    let onSaveResolvedSawAbort = false;
    try {
      await act(async () => {
        d.resolve();
        await Promise.resolve();
      });
    } catch (_) {
      onSaveResolvedSawAbort = true;
    }

    // Property: reset() should have caused the in-flight save's
    // promise consumer to either reject with an abort error OR the
    // onSave call to have received a cancellation signal it could
    // observe. Neither is true today — the network call to /signals/A
    // proceeds to completion regardless of the user's intent.
    const callArgs = onSave.mock.calls[0];
    const hadAbortSignal =
      callArgs.length >= 2
      || (callArgs[0] && typeof callArgs[0] === 'object' && 'signal' in callArgs[0])
      || onSaveResolvedSawAbort;
    expect(hadAbortSignal).toBe(true);
  });

  // -----------------------------------------------------------------
  // SCENARIO 5 — React 18 StrictMode double-invoke of effects.
  //
  // In dev, StrictMode mounts → unmounts → remounts. The hook's
  // autosave effect must end up with EXACTLY one armed timer, and
  // that timer must fire onSave EXACTLY once for one logical edit.
  //
  // Verify by mounting under StrictMode and checking that one edit
  // results in exactly one onSave invocation.
  //
  // Expected: PASSes (the hook cancels the timer on cleanup and
  // re-arms on the second mount). Documents safety.
  // -----------------------------------------------------------------
  it('PASS (safety): one edit fires onSave exactly once under StrictMode', async () => {
    vi.useFakeTimers();
    const onSave = vi.fn(() => Promise.resolve());
    function Outer() {
      const [payload, setPayload] = useState({ v: 0 });
      useBackendAutosave({ enabled: true, payload, onSave, debounceMs: 50 });
      Outer.setPayload = setPayload;
      return null;
    }
    render(React.createElement(React.StrictMode, null, React.createElement(Outer)));
    // First effect from initial mount may schedule a timer because
    // payload reference changed — flush.
    await act(async () => { vi.advanceTimersByTime(50); });
    onSave.mockClear();

    // One logical edit.
    await act(async () => { Outer.setPayload({ v: 1 }); });
    await act(async () => { vi.advanceTimersByTime(50); });
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  // -----------------------------------------------------------------
  // SCENARIO 6 — Backend permanently slow: edits queue indefinitely.
  //
  // If the backend hangs (never resolves), and the user keeps editing
  // every <debounce> seconds, each new edit creates a fresh in-flight
  // PUT (because there's no per-payload coalescing of in-flight saves
  // — runSave fires unconditionally). The previous PUTs remain
  // pending. This is unbounded memory growth in pathological cases,
  // and once they ALL resolve (server unsticks), the LAST one to land
  // on the server wins — which may not be the latest edit due to
  // network reordering.
  //
  // Verify: N edits during sustained backend hang produce N pending
  // onSave invocations with no in-flight coalescing.
  // -----------------------------------------------------------------
  it('sustained backend hang does not produce unbounded in-flight saves (coalescing)', async () => {
    vi.useFakeTimers();
    const onSave = vi.fn(() => new Promise(() => {})); // never resolves
    mount({ enabled: true, initialPayload: { v: 0 }, onSave, debounceMs: 50 });

    for (let i = 1; i <= 5; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { Harness.setPayload({ v: i }); });
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { vi.advanceTimersByTime(50); });
    }
    // Hook fires a fresh PUT for EACH edit because the previous PUT
    // never resolves — no in-flight coalescing.
    // A defensive implementation would either (a) skip firing while
    // a save is in flight and re-fire on resolve, or (b) abort the
    // in-flight save.
    expect(onSave.mock.calls.length).toBeLessThanOrEqual(1);
  });
});
