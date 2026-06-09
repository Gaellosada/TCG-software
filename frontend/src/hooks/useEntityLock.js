import { useCallback } from 'react';

/**
 * Shared lock/unlock handler for the three persisted-entity pages
 * (Signals / Indicators / Portfolio). Each page has a near-identical
 * "set the locked flag on the backend, then reconcile local state"
 * handler; this hook captures that shape without dictating HOW a page
 * surfaces status or stores its locked flag.
 *
 * Behaviour (matches the pre-extraction handlers exactly):
 *   1. ``onStart()`` — page marks its save indicator as in-flight.
 *   2. (optional) ``applyLocked(id, next)`` — OPTIMISTIC local flip so the
 *      toggle responds immediately. Only call this for pages that did an
 *      optimistic update before (Indicators); omit it for pages that
 *      waited for the server (Signals / Portfolio).
 *   3. ``await setLocked(id, next)`` — the ``/lock`` API call.
 *   4. On success: ``applyLocked(id, serverLocked, doc)`` with the server's
 *      canonical ``locked`` (falling back to ``next`` when the server omits
 *      it), then ``onSuccess(doc)``.
 *   5. On failure: when ``applyLocked`` was used optimistically, roll the
 *      flag back to ``!next``; then ``onError(err)``.
 *
 * The hook owns ONLY the call + try/catch + optimistic/rollback wiring.
 * Status text, error messages, and which collection holds the flag stay
 * with the page via the injected callbacks — so observable behaviour is
 * identical to the hand-rolled handlers it replaces.
 *
 * @param {object}   opts
 * @param {Function} opts.setLocked   ``(id, next) => Promise<doc>`` — the
 *   persistence ``setXLocked`` API function.
 * @param {Function} opts.applyLocked ``(id, lockedVal, doc?) => void`` —
 *   write the locked flag into the page's local state for ``id``.
 * @param {boolean}  [opts.optimistic=false]  When true, ``applyLocked`` is
 *   called BEFORE the request (and rolled back on failure).
 * @param {Function} [opts.onStart]   ``() => void`` — before the request.
 * @param {Function} [opts.onSuccess] ``(doc) => void`` — after a successful
 *   request (the server doc is passed through).
 * @param {Function} [opts.onError]   ``(err) => void`` — after a failed
 *   request (rollback, if any, has already run).
 * @returns {(id: string, next: boolean) => Promise<void>}
 */
export default function useEntityLock({
  setLocked,
  applyLocked,
  optimistic = false,
  onStart,
  onSuccess,
  onError,
}) {
  return useCallback(async (id, next) => {
    if (onStart) onStart();
    // Optimistic flip (Indicators) — show the new state immediately.
    if (optimistic && applyLocked) applyLocked(id, next);
    try {
      const doc = await setLocked(id, next);
      const serverLocked = doc && typeof doc.locked === 'boolean' ? doc.locked : next;
      if (applyLocked) applyLocked(id, serverLocked, doc);
      if (onSuccess) onSuccess(doc);
    } catch (err) {
      // Roll back the optimistic flip so the toggle returns to its prior state.
      if (optimistic && applyLocked) applyLocked(id, !next);
      if (onError) onError(err);
    }
  }, [setLocked, applyLocked, optimistic, onStart, onSuccess, onError]);
}
