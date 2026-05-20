import { useEffect, useRef, useState, useCallback } from 'react';

/**
 * Default debounce duration for backend autosave (ms).
 *
 * Callers should omit ``debounceMs`` from ``useBackendAutosave`` rather than
 * hardcoding a value, so any future tuning of this constant propagates
 * automatically.
 */
export const DEFAULT_AUTOSAVE_DEBOUNCE_MS = 3000;

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
 *   3. Unmounting aborts the in-flight save and prevents post-unmount
 *      state updates.
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
 *             reset: () => void,
 *             setStatus: (s: 'idle'|'saving'|'saved'|'error') => void }}
 *   ``flush`` synchronously fires any pending debounced save (coalesces
 *   with in-flight per the rules above).
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
  const onSaveRef = useRef(onSave);
  const payloadRef = useRef(payload);
  // Active AbortController for the in-flight save, or null if none.
  const controllerRef = useRef(null);
  // True when an edit lands while a save is in flight — we'll fire a
  // new save with the latest payload after the in-flight one settles.
  const pendingRestartRef = useRef(false);
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

  // Internal: actually fire onSave. Assumes controllerRef is null (no
  // save currently in flight).
  const launchSave = useCallback(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    if (mountedRef.current) setStatus('saving');
    const value = payloadRef.current;
    Promise.resolve()
      .then(() => onSaveRef.current(value, { signal: controller.signal }))
      .then(() => {
        // Only resolve status if this controller is still the active one.
        if (controllerRef.current !== controller) return;
        controllerRef.current = null;
        if (pendingRestartRef.current) {
          // A newer edit was queued while we were in flight — fire it.
          pendingRestartRef.current = false;
          if (launchSaveRef.current) launchSaveRef.current();
          return;
        }
        if (mountedRef.current) setStatus('saved');
      })
      .catch((err) => {
        const isAbort =
          (err && (err.name === 'AbortError' || err.code === 20))
          || controller.signal.aborted;
        const wasActive = controllerRef.current === controller;
        if (wasActive) controllerRef.current = null;
        if (!mountedRef.current) return;
        // If a restart is queued, fire it regardless of how this save
        // ended — the user's latest intent is what matters.
        if (wasActive && pendingRestartRef.current) {
          pendingRestartRef.current = false;
          if (launchSaveRef.current) launchSaveRef.current();
          return;
        }
        if (!wasActive) return; // superseded — caller already moved on
        if (isAbort) {
          setStatus('idle');
        } else {
          setStatus('error');
        }
      });
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
    pendingRestartRef.current = false;
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
    runSave();
  }, [cancelTimer, runSave]);

  useEffect(() => {
    if (!enabled) {
      cancelTimer();
      return undefined;
    }
    cancelTimer();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      runSave();
    }, debounceMs);
    return cancelTimer;
  }, [enabled, payload, debounceMs, cancelTimer, runSave]);

  // Cleanup on unmount: cancel timer, abort in-flight, mark unmounted.
  useEffect(() => () => {
    mountedRef.current = false;
    cancelTimer();
    pendingRestartRef.current = false;
    const c = controllerRef.current;
    if (c) {
      controllerRef.current = null;
      try { c.abort(); } catch { /* ignore */ }
    }
  }, [cancelTimer]);

  return { status, flush, reset, setStatus };
}
