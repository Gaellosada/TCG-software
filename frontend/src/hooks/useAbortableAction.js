import { useState, useCallback, useRef, useEffect } from 'react';

/**
 * Hook managing the AbortController + ``running`` flag lifecycle
 * shared by IndicatorsPage.runIndicator, SignalsPage.handleRun, and
 * usePortfolio.handleCalculate.
 *
 * ``run(asyncFn)`` aborts any in-flight controller, creates a new one,
 * invokes ``asyncFn({signal})`` and toggles ``running`` around the call.
 * ``abort()`` cancels the current controller and clears the running
 * flag. The hook also aborts on unmount.
 *
 * Page-specific ``useEffect(() => () => abort(), [selectedId])``
 * patterns continue to work: they just call ``abort()`` here rather
 * than poking at a controller ref.
 *
 * Returns the raw ``asyncFn`` return value so the caller can branch
 * on the result. Errors (including aborted fetches) propagate so the
 * call-site keeps its existing try/catch error-classification shape.
 */
export default function useAbortableAction() {
  const [running, setRunning] = useState(false);
  const controllerRef = useRef(null);

  const abort = useCallback(() => {
    if (controllerRef.current) {
      controllerRef.current.abort();
      controllerRef.current = null;
      setRunning(false);
    }
  }, []);

  // Abort on unmount — never leave a background request alive after
  // the component has gone away.
  useEffect(() => () => {
    if (controllerRef.current) controllerRef.current.abort();
  }, []);

  const run = useCallback(async (asyncFn) => {
    // Cancel any still-running call so a stale response can't clobber
    // state after a new one kicks off.
    if (controllerRef.current) controllerRef.current.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setRunning(true);
    try {
      return await asyncFn({ signal: controller.signal });
    } finally {
      // Only flip ``running`` off when this specific call wasn't
      // aborted. If the user hit Run again mid-flight, the new
      // invocation already set running=true; clearing it here would
      // falsely toggle the UI off.
      if (!controller.signal.aborted) setRunning(false);
      if (controllerRef.current === controller) controllerRef.current = null;
    }
  }, []);

  return { run, running, abort };
}
