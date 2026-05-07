import { useEffect, useRef, useCallback } from 'react';

/**
 * Issue 28b — Unfocused tab title alert + OS Notifications API
 *
 * Behaviour:
 * - When the page is hidden (Page Visibility API: document.hidden) AND a
 *   target event fires, prepend " ● " to document.title.
 * - When the tab regains focus (visibilitychange → not hidden), restore the
 *   clean title.
 * - OS Notification: permission is requested only after the FIRST turn_complete
 *   (G-NOTIF-PERM). If user already dismissed ("denied" or localStorage flag),
 *   we do not re-ask. On subsequent target events when permission === "granted"
 *   AND page is hidden, fire a Notification with stable tag to coalesce.
 *
 * Target events (mapped to notification body text):
 *   turn_complete         → "Task complete"
 *   auto_continue_capped  → "Auto-continue cap reached — needs attention"
 *   notebook_ready        → "Notebook ready — view results"
 *   notebook_failed       → "Notebook compilation failed — no outputs"
 *
 * Usage: call at the AgentPage level, passing current values from
 *   useAgentSession plus a stable string for each trigger condition.
 *
 * @param {{
 *   lastTurnComplete: object|null,
 *   autoContinueCapped: object|null,
 *   notebookReady: boolean,
 *   notebookFailedInfo: object|null,
 * }} triggers
 */
export function useUnfocusedTitleAlert({
  lastTurnComplete,
  autoContinueCapped,
  notebookReady,
  notebookFailedInfo,
}) {
  // Store the clean title so we can restore it on focus.
  const cleanTitleRef = useRef(null);
  // Track whether we've asked in *this page load*. Across reloads we rely on
  // Notification.permission itself ('default' = browser will prompt; 'granted'
  // = already given; 'denied' = user blocked) — see C2 audit
  // R-NOTIF-PERM-NO-RECOVERY: a sticky localStorage flag was previously
  // tested but it left users with no recovery path after a single dismiss.
  const permissionAskedRef = useRef(false);

  // Initialise the ref on first render.
  if (cleanTitleRef.current === null) {
    cleanTitleRef.current = document.title;
  }

  // Restore title when tab becomes visible again.
  useEffect(() => {
    function handleVisibility() {
      if (!document.hidden && cleanTitleRef.current !== null) {
        document.title = cleanTitleRef.current;
      }
    }
    document.addEventListener('visibilitychange', handleVisibility);
    return () => document.removeEventListener('visibilitychange', handleVisibility);
  }, []);

  /**
   * Request OS Notification permission if it's still 'default' and we
   * haven't already asked in this page load. Browser itself prevents the
   * dialog from spamming the user (subsequent requestPermission() calls
   * after a dismiss return current state without showing UI again until
   * the page reloads). On reload, the user gets one fresh chance —
   * recoverable, unlike the prior sticky-localStorage gate.
   */
  const maybeRequestPermission = useCallback(() => {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission === 'granted') return;
    if (Notification.permission === 'denied') return;
    if (permissionAskedRef.current) return;
    permissionAskedRef.current = true;
    Notification.requestPermission();
  }, []);

  /**
   * Fire a tab-title alert + OS notification for a given message.
   * Only acts when the page is currently hidden.
   */
  const fireAlert = useCallback(
    (notifTitle, notifBody) => {
      if (!document.hidden) return;

      // Tab title: static prefix (no blink). Leading space is trimmed by some
      // environments (JSDOM); use non-spaced prefix for cross-env consistency.
      if (!document.title.startsWith('● ')) {
        cleanTitleRef.current = document.title;
        document.title = `● ${cleanTitleRef.current}`;
      }

      // OS Notification — only when permission granted.
      if (
        typeof Notification !== 'undefined' &&
        Notification.permission === 'granted'
      ) {
        // eslint-disable-next-line no-new
        new Notification(notifTitle, {
          body: notifBody,
          tag: 'agent-task-complete', // stable tag → notifications coalesce
          icon: '/favicon.ico',
        });
      }
    },
    [],
  );

  // turn_complete: request permission (G-NOTIF-PERM) then fire alert.
  useEffect(() => {
    if (!lastTurnComplete) return;
    maybeRequestPermission();
    fireAlert('Task complete', 'Task complete');
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastTurnComplete]);

  // auto_continue_capped: alert without requesting permission (permission was
  // requested on the prior turn_complete).
  useEffect(() => {
    if (!autoContinueCapped) return;
    fireAlert(
      'Auto-continue cap reached',
      'Auto-continue cap reached — needs attention',
    );
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoContinueCapped]);

  // notebook_ready: alert.
  useEffect(() => {
    if (!notebookReady) return;
    fireAlert('Notebook ready', 'Notebook ready — view results');
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [notebookReady]);

  // notebook_failed: alert.
  useEffect(() => {
    if (!notebookFailedInfo) return;
    fireAlert(
      'Notebook compilation failed',
      'Notebook compilation failed — no outputs',
    );
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [notebookFailedInfo]);
}
