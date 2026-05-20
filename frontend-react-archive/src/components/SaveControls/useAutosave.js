import { useEffect, useRef } from 'react';

/**
 * Debounced autosave hook.
 *
 * Options:
 *   enabled     {boolean}   gate — when false, hook is a no-op
 *   dirty       {boolean}   only schedules a save when true
 *   value       {any}       payload passed to onSave — included in the deps
 *                           so a change reschedules the debounce
 *   onSave      {Function}  (value) => void (or sync); MUST be stable or
 *                           wrapped in useCallback — we always invoke the
 *                           latest version via a ref
 *   debounceMs  {number}    debounce delay in milliseconds (default 500)
 *
 * Behaviour:
 *   - When ``enabled && dirty``, (re)schedules a debounced ``onSave(value)``.
 *   - On unmount OR when ``enabled`` flips to false, any pending timer
 *     is cancelled WITHOUT firing (avoid surprise writes).
 *   - Installs ``beforeunload`` + ``pagehide`` listeners while enabled
 *     that FLUSH any pending timer synchronously before the page tears
 *     down. Removed when ``enabled`` is false or on unmount.
 */
export default function useAutosave({
  enabled,
  dirty,
  value,
  onSave,
  debounceMs = 500,
}) {
  const timerRef = useRef(null);
  const pendingRef = useRef(null); // { value } — null means "nothing scheduled"
  const onSaveRef = useRef(onSave);

  useEffect(() => {
    onSaveRef.current = onSave;
  }, [onSave]);

  // Schedule / reschedule the debounced save.
  useEffect(() => {
    if (!enabled || !dirty) return undefined;
    pendingRef.current = { value };
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      const snapshot = pendingRef.current;
      timerRef.current = null;
      pendingRef.current = null;
      if (snapshot) onSaveRef.current(snapshot.value);
    }, debounceMs);
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      // Intentionally leave pendingRef untouched — flush() still needs
      // access to the most-recent scheduled payload in the unload path.
      // The listener cleanup below removes the listener, so flush only
      // fires while the hook is enabled.
    };
  }, [enabled, dirty, value, debounceMs]);

  // Install the flush-on-unload listeners only while enabled.
  useEffect(() => {
    if (!enabled) return undefined;
    const flush = () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      const snapshot = pendingRef.current;
      pendingRef.current = null;
      if (snapshot) {
        try { onSaveRef.current(snapshot.value); } catch { /* swallow — we're unloading */ }
      }
    };
    // ``beforeunload`` covers reload/close in most browsers; ``pagehide``
    // covers Safari / iOS bfcache where ``beforeunload`` is suppressed.
    window.addEventListener('beforeunload', flush);
    window.addEventListener('pagehide', flush);
    return () => {
      window.removeEventListener('beforeunload', flush);
      window.removeEventListener('pagehide', flush);
    };
  }, [enabled]);
}
