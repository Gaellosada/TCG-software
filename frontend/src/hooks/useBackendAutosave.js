import { useEffect, useRef, useState, useCallback } from 'react';

/**
 * Default debounce duration for backend autosave (ms).
 *
 * Callers should omit ``debounceMs`` from ``useBackendAutosave`` rather than
 * hardcoding a value, so any future tuning of this constant propagates
 * automatically.
 */
export const DEFAULT_AUTOSAVE_DEBOUNCE_MS = 3000;

// Externally-settled promise. Lets ``saveNow`` hand back a promise that
// resolves only when a COALESCED restart (not the prior in-flight save it
// piggy-backed on) has persisted the override payload.
function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/**
 * Debounced backend-autosave hook with AbortController + in-flight
 * coalescing.
 *
 * Calls ``onSave(payload, { signal })`` after ``debounceMs`` of inactivity
 * on ``payload``. The caller is REQUIRED to thread ``signal`` into the
 * underlying ``fetch`` so that aborts cancel the wire-level request, not
 * just the status update.
 *
 * Concurrency model: at most ONE in-flight save per hook instance.
 *
 *   1. While a save is in flight and the debounce fires again, the hook
 *      does NOT dispatch a second concurrent ``onSave`` call. Instead it
 *      records ``pendingRestart=true`` (the very last debounce wins) and
 *      starts a new save with the latest payload IMMEDIATELY AFTER the
 *      in-flight one settles. The net wire traffic for a sustained
 *      backend hang is exactly one request (until it eventually
 *      resolves) — no unbounded queue, no out-of-order writes.
 *
 *   2. ``reset()`` aborts the in-flight save (cancelling the wire
 *      request) AND clears any ``pendingRestart`` so the switch-away is
 *      truly idempotent. Used on selection change.
 *
 *   3. Unmounting FLUSHES a pending debounced edit (fires the save so a
 *      last edit is never lost on navigation) and does NOT abort an
 *      in-flight save (it is allowed to complete); it only prevents
 *      post-unmount state updates. Aborting on context switch is
 *      ``reset()``'s job (rule 2), not unmount's.
 *
 *   4. Status semantics: an AbortError-rejected save resolves the status
 *      to ``'idle'`` (it was intentionally cancelled), unless another
 *      save took over in the meantime (in which case the new save's
 *      ``'saving'`` is already showing — leave it alone). A non-abort
 *      rejection resolves to ``'error'``. Success resolves to ``'saved'``.
 *
 * @param {object} opts
 * @param {boolean} opts.enabled  Gate — when false, the hook is a no-op
 *   and any pending timer is cancelled WITHOUT firing.
 * @param {*}       opts.payload  Value passed to ``onSave``. A reference
 *   change reschedules the debounce.
 * @param {Function} opts.onSave  ``(payload, { signal }) => Promise<*>``.
 *   Called with the latest payload after the debounce. The caller MUST
 *   thread ``signal`` into the underlying ``fetch``. Failure (rejected
 *   Promise) sets status to ``'error'`` unless the rejection is an
 *   ``AbortError`` (which maps to ``'idle'``).
 * @param {number}  [opts.debounceMs=DEFAULT_AUTOSAVE_DEBOUNCE_MS]
 * @returns {{ status: 'idle'|'saving'|'saved'|'error',
 *             flush: () => void,
 *             saveNow: (overridePayload?: *) => Promise<*>,
 *             reset: () => void,
 *             setStatus: (s: 'idle'|'saving'|'saved'|'error') => void }}
 *   ``flush`` synchronously fires any pending debounced save (coalesces
 *   with in-flight per the rules above).
 *   ``saveNow`` fires a save UNCONDITIONALLY (even with autosave off /
 *   no timer pending), cancels the debounce, coalesces with any in-flight
 *   save, and returns a Promise that settles when the save completes.
 *   Pass ``overridePayload`` to persist an explicit payload (guards the
 *   stale-state race where a synchronous setState hasn't propagated yet).
 *   ``reset`` clears the status back to ``idle``, cancels any pending
 *   timer WITHOUT firing, AND aborts the in-flight save if any — use
 *   when switching selection.
 *   ``setStatus`` allows one-shot mutation handlers (add / archive /
 *   category-change) to reflect their own save state through the same
 *   indicator without going through the debounce path.
 */
export default function useBackendAutosave({
  enabled,
  payload,
  onSave,
  debounceMs = DEFAULT_AUTOSAVE_DEBOUNCE_MS,
}) {
  const [status, setStatus] = useState('idle');
  const timerRef = useRef(null);
  // True while a debounced save is armed but has not yet fired. Kept
  // SEPARATE from ``timerRef`` because the scheduling effect's cleanup
  // nulls ``timerRef`` on unmount BEFORE the unmount-flush effect runs —
  // this ref survives that so the flush can tell there was pending work.
  const pendingRef = useRef(false);
  const onSaveRef = useRef(onSave);
  const payloadRef = useRef(payload);
  // Active AbortController for the in-flight save, or null if none.
  const controllerRef = useRef(null);
  // True when an edit lands while a save is in flight — we'll fire a
  // new save with the latest payload after the in-flight one settles.
  const pendingRestartRef = useRef(false);
  // Promise of the current in-flight save (or null). Lets ``saveNow``
  // return something awaitable when it coalesces with an in-flight save.
  const inFlightPromiseRef = useRef(null);
  // Deferred that settles when a QUEUED restart (from ``saveNow`` coalescing)
  // eventually persists its payload — so ``saveNow`` awaiters are signalled
  // only after the override is durable, not when the prior save settles.
  const pendingRestartDeferredRef = useRef(null);
  // Track mounted state to guard setState across async boundaries.
  const mountedRef = useRef(true);

  useEffect(() => { onSaveRef.current = onSave; }, [onSave]);
  useEffect(() => { payloadRef.current = payload; }, [payload]);

  const cancelTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // Forward declaration via ref to break the runSave -> launchSave cycle.
  const launchSaveRef = useRef(null);

  // Fire a queued restart (last-edit-wins) and, if a ``saveNow`` awaiter
  // coalesced onto it, settle that awaiter's deferred only when the restart's
  // own save chain settles — so the promise resolves after the OVERRIDE
  // payload is persisted, not when the piggy-backed save finished. Reads
  // ``launchSaveRef`` (a ref) so it can be defined before ``launchSave``.
  const fireQueuedRestart = useCallback(() => {
    pendingRestartRef.current = false;
    const restartPromise = launchSaveRef.current
      ? launchSaveRef.current()
      : Promise.resolve();
    const d = pendingRestartDeferredRef.current;
    if (d) {
      pendingRestartDeferredRef.current = null;
      // launchSave's promise resolves when this restart's onSave settles
      // (and, if IT coalesces again, chains onward). Never rejects the
      // awaiter for an internal restart hiccup — fall back to resolve.
      Promise.resolve(restartPromise).then(d.resolve, d.resolve);
    }
  }, []);

  // Internal: actually fire onSave. Assumes controllerRef is null (no
  // save currently in flight).
  const launchSave = useCallback(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    if (mountedRef.current) setStatus('saving');
    const value = payloadRef.current;
    const promise = Promise.resolve()
      .then(() => onSaveRef.current(value, { signal: controller.signal }))
      .then(() => {
        // Only resolve status if this controller is still the active one.
        if (controllerRef.current !== controller) return;
        controllerRef.current = null;
        if (pendingRestartRef.current) {
          // A newer edit was queued while we were in flight — fire it.
          fireQueuedRestart();
          return;
        }
        if (inFlightPromiseRef.current === promise) inFlightPromiseRef.current = null;
        if (mountedRef.current) setStatus('saved');
      })
      .catch((err) => {
        const isAbort =
          (err && (err.name === 'AbortError' || err.code === 20))
          || controller.signal.aborted;
        const wasActive = controllerRef.current === controller;
        if (wasActive) controllerRef.current = null;
        // If a restart is queued, fire it regardless of how this save
        // ended — the user's latest intent is what matters. This runs
        // even after unmount so a pending edit flushed on navigation is
        // not dropped when the preceding in-flight save fails.
        if (wasActive && pendingRestartRef.current) {
          fireQueuedRestart();
          return;
        }
        if (inFlightPromiseRef.current === promise) inFlightPromiseRef.current = null;
        if (!mountedRef.current) return;
        if (!wasActive) return; // superseded — caller already moved on
        if (isAbort) {
          setStatus('idle');
        } else {
          setStatus('error');
        }
      });
    inFlightPromiseRef.current = promise;
    return promise;
  }, []);

  useEffect(() => { launchSaveRef.current = launchSave; }, [launchSave]);

  // Public: schedule a save now. If a save is in flight, mark
  // pendingRestart and let the in-flight one finish first (coalescing).
  const runSave = useCallback(() => {
    if (controllerRef.current) {
      pendingRestartRef.current = true;
      return;
    }
    launchSave();
  }, [launchSave]);

  const reset = useCallback(() => {
    cancelTimer();
    pendingRef.current = false;
    pendingRestartRef.current = false;
    inFlightPromiseRef.current = null;
    // A queued restart is being discarded (context switch, not data loss) —
    // settle any ``saveNow`` awaiter so its promise never dangles.
    if (pendingRestartDeferredRef.current) {
      const d = pendingRestartDeferredRef.current;
      pendingRestartDeferredRef.current = null;
      d.resolve();
    }
    const c = controllerRef.current;
    if (c) {
      controllerRef.current = null;
      try { c.abort(); } catch { /* ignore */ }
    }
    if (mountedRef.current) setStatus('idle');
  }, [cancelTimer]);

  const flush = useCallback(() => {
    if (!timerRef.current) return;
    cancelTimer();
    pendingRef.current = false;
    runSave();
  }, [cancelTimer, runSave]);

  // Public: fire a save RIGHT NOW, unconditionally — used by the manual
  // Save button. Unlike ``flush`` (which no-ops unless a debounce timer
  // is pending), ``saveNow`` always dispatches, so it works when autosave
  // is OFF (no timer is ever scheduled) and when the debounce already
  // fired. Cancels any pending timer, coalesces with an in-flight save
  // (last-edit-wins), and returns a Promise that settles when the save
  // completes so callers can await persistence.
  //
  // ``overridePayload`` (optional): when provided, persist THIS payload
  // instead of the tracked ``payload`` prop. Guards the stale-state race
  // where a synchronous ``setState`` (e.g. a rename) has not yet
  // propagated into the ``payload`` prop by click time.
  const saveNow = useCallback((overridePayload) => {
    cancelTimer();
    pendingRef.current = false;
    if (overridePayload !== undefined) {
      payloadRef.current = overridePayload;
    }
    if (controllerRef.current) {
      // A save is already in flight — queue the latest payload to fire after
      // it settles. Hand back a promise tied to the QUEUED RESTART (not the
      // prior in-flight save), so an awaiter is signalled only once THIS
      // override payload is durably persisted (matches the JSDoc contract).
      pendingRestartRef.current = true;
      if (!pendingRestartDeferredRef.current) {
        pendingRestartDeferredRef.current = createDeferred();
      }
      return pendingRestartDeferredRef.current.promise;
    }
    return launchSave();
  }, [cancelTimer, launchSave]);

  useEffect(() => {
    if (!enabled) {
      cancelTimer();
      pendingRef.current = false;
      return undefined;
    }
    cancelTimer();
    pendingRef.current = true;
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      pendingRef.current = false;
      runSave();
    }, debounceMs);
    // Cleanup clears only the timer (on re-arm or unmount); pendingRef is
    // intentionally left set so the unmount-flush effect can still see it.
    return cancelTimer;
  }, [enabled, payload, debounceMs, cancelTimer, runSave]);

  // Cleanup on unmount (BUG 2 — SPA navigation must NOT lose data):
  //
  //   - If a debounced save is pending, FLUSH it — fire the save now so
  //     the unsaved edit is persisted instead of being silently dropped
  //     when React unmounts the page on a route change. The request is
  //     allowed to complete (the SPA process stays alive); status updates
  //     are skipped since the component is gone.
  //   - Do NOT abort an in-flight save — it represents unsaved user data.
  //     If a newer edit is also pending, queue it so it fires after the
  //     in-flight save settles.
  //
  // This is deliberately different from ``reset()`` (selection switch),
  // which DOES abort — that is a context switch, not data loss.
  //
  // The setup body re-arms ``mountedRef`` to true on every (re)mount. Without
  // it, ``mountedRef`` (initialised true) is set false by the cleanup on the
  // first teardown and NEVER restored — so under React StrictMode's
  // mount→unmount→remount probe (and any real remount) all subsequent
  // ``setStatus`` calls are silently skipped and the save-status indicator
  // ('saving'/'saved') never appears even though the save fires. Deps are
  // stable ([] useCallbacks) so this only runs on true (re)mounts.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      const hadPending = pendingRef.current;
      pendingRef.current = false;
      cancelTimer();
      if (hadPending) {
        if (controllerRef.current) {
          // In-flight save exists — fire the pending edit right after it.
          pendingRestartRef.current = true;
        } else {
          launchSave();
        }
      } else {
        pendingRestartRef.current = false;
      }
      // NOTE: intentionally leave controllerRef alone — the in-flight
      // request (if any) is allowed to complete.
    };
  }, [cancelTimer, launchSave]);

  return {
    status, flush, saveNow, reset, setStatus,
  };
}
