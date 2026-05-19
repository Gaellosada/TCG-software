import { useEffect, useRef, useState, useCallback } from 'react';

/**
 * Debounced backend-autosave hook.
 *
 * Calls ``onSave(payload)`` after ``debounceMs`` of inactivity on
 * ``payload``. Tracks save status for a small UI indicator.
 *
 * Concurrency model: the LAST edit wins. If a save is in-flight and a
 * new edit lands, the result of the in-flight save is IGNORED — only
 * the result of the most recently invoked save updates the status.
 * Implemented via a monotonically-increasing token (``saveSeqRef``)
 * captured by each save invocation; only the highest-numbered call may
 * update status.
 *
 * @param {object} opts
 * @param {boolean} opts.enabled  Gate — when false, the hook is a no-op
 *   and any pending timer is cancelled WITHOUT firing.
 * @param {*}       opts.payload  Value passed to ``onSave``. A reference
 *   change reschedules the debounce.
 * @param {Function} opts.onSave  ``(payload) => Promise<*>``. Called with
 *   the latest payload after the debounce. Failure (rejected Promise)
 *   sets status to ``'error'``.
 * @param {number}  [opts.debounceMs=500]
 * @returns {{ status: 'idle'|'saving'|'saved'|'error',
 *             flush: () => void,
 *             reset: () => void,
 *             setStatus: (s: 'idle'|'saving'|'saved'|'error') => void }}
 *   ``flush`` synchronously fires any pending debounced save.
 *   ``reset`` clears the status back to ``idle`` and cancels any
 *   pending timer WITHOUT firing — use when switching selection so the
 *   indicator doesn't show "saved" for the wrong item.
 *   ``setStatus`` allows one-shot mutation handlers (add / archive /
 *   category-change) to reflect their own save state through the same
 *   indicator without going through the debounce path.
 */
export default function useBackendAutosave({
  enabled,
  payload,
  onSave,
  debounceMs = 500,
}) {
  const [status, setStatus] = useState('idle');
  const timerRef = useRef(null);
  const onSaveRef = useRef(onSave);
  const payloadRef = useRef(payload);
  // Token bumped on every scheduled call. Only the highest token may
  // update status — older in-flight saves are ignored on completion.
  const saveSeqRef = useRef(0);
  // Track the last token that has STARTED — so reset() can compare and
  // abandon results from older inflight saves.
  const lastStartedSeqRef = useRef(0);

  useEffect(() => { onSaveRef.current = onSave; }, [onSave]);
  useEffect(() => { payloadRef.current = payload; }, [payload]);

  const cancelTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const reset = useCallback(() => {
    cancelTimer();
    // Bump the seq so any in-flight save's result is ignored.
    saveSeqRef.current += 1;
    setStatus('idle');
  }, [cancelTimer]);

  const runSave = useCallback(() => {
    saveSeqRef.current += 1;
    const mySeq = saveSeqRef.current;
    lastStartedSeqRef.current = mySeq;
    setStatus('saving');
    const value = payloadRef.current;
    Promise.resolve()
      .then(() => onSaveRef.current(value))
      .then(() => {
        if (mySeq === saveSeqRef.current) setStatus('saved');
      })
      .catch(() => {
        if (mySeq === saveSeqRef.current) setStatus('error');
      });
  }, []);

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

  // Cleanup on unmount.
  useEffect(() => cancelTimer, [cancelTimer]);

  return { status, flush, reset, setStatus };
}
