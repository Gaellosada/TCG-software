import { DestroyRef, Injectable, Signal, effect, inject, signal, untracked } from '@angular/core';

export type TcgSaveStatus = 'idle' | 'saving' | 'saved' | 'error';

export const TCG_DEFAULT_AUTOSAVE_DEBOUNCE_MS = 3000;

export interface TcgBackendAutosaveRegistration<T> {
  enabled: Signal<boolean>;
  payload: Signal<T>;
  onSave: (payload: T, opts: { signal: AbortSignal }) => Promise<unknown>;
  debounceMs?: number;
}

export interface TcgBackendAutosaveHandle {
  readonly status: Signal<TcgSaveStatus>;
  flush(): void;
  reset(): void;
  setStatus(status: TcgSaveStatus): void;
  dispose(): void;
}

/**
 * Debounced backend-autosave service. Mirrors React's
 * `useBackendAutosave.js` line-by-line — the production-VIX semantics are
 * load-bearing and the `race.test.jsx` test cases pin every branch.
 *
 * Concurrency model:
 *  1. Default debounce: `TCG_DEFAULT_AUTOSAVE_DEBOUNCE_MS` (3000 ms).
 *  2. AT MOST ONE in-flight save per `register()` call. While in flight,
 *     a fresh payload change sets `pendingRestart = true`; when the
 *     in-flight save settles, a new save fires with the latest payload.
 *  3. `reset()` aborts the in-flight `AbortController`, clears the timer
 *     and the pendingRestart flag.
 *  4. AbortError-rejected save → status `'idle'` (unless superseded);
 *     non-abort rejection → `'error'`; resolved → `'saved'`.
 *  5. Cleanup ties the handle's `dispose()` to the caller's `DestroyRef`.
 *
 * G5: component-scoped. Provide via `providers: [TcgBackendAutosaveService]`
 * on the host component.
 */
@Injectable()
export class TcgBackendAutosaveService {
  register<T>(reg: TcgBackendAutosaveRegistration<T>): TcgBackendAutosaveHandle {
    const destroyRef = inject(DestroyRef);

    const debounceMs = reg.debounceMs ?? TCG_DEFAULT_AUTOSAVE_DEBOUNCE_MS;
    const status = signal<TcgSaveStatus>('idle');

    let timer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;
    let pendingRestart = false;
    let mounted = true;

    const cancelTimer = (): void => {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };

    // Internal: actually fire onSave. Assumes `controller` is null.
    const launchSave = (): void => {
      const c = new AbortController();
      controller = c;
      if (mounted) status.set('saving');
      const value = untracked(() => reg.payload());
      Promise.resolve()
        .then(() => reg.onSave(value, { signal: c.signal }))
        .then(() => {
          if (controller !== c) return;
          controller = null;
          if (pendingRestart) {
            pendingRestart = false;
            launchSave();
            return;
          }
          if (mounted) status.set('saved');
        })
        .catch((err: unknown) => {
          const isAbort =
            (err && typeof err === 'object' &&
              ((err as { name?: string }).name === 'AbortError' ||
                (err as { code?: number }).code === 20)) ||
            c.signal.aborted;
          const wasActive = controller === c;
          if (wasActive) controller = null;
          if (!mounted) return;
          if (wasActive && pendingRestart) {
            pendingRestart = false;
            launchSave();
            return;
          }
          if (!wasActive) return;
          if (isAbort) status.set('idle');
          else status.set('error');
        });
    };

    const runSave = (): void => {
      if (controller) {
        pendingRestart = true;
        return;
      }
      launchSave();
    };

    const flush = (): void => {
      if (!timer) return;
      cancelTimer();
      runSave();
    };

    const reset = (): void => {
      cancelTimer();
      pendingRestart = false;
      const c = controller;
      if (c) {
        controller = null;
        try {
          c.abort();
        } catch {
          /* ignore */
        }
      }
      if (mounted) status.set('idle');
    };

    const setStatus = (s: TcgSaveStatus): void => {
      if (mounted) status.set(s);
    };

    const dispose = (): void => {
      mounted = false;
      cancelTimer();
      pendingRestart = false;
      const c = controller;
      if (c) {
        controller = null;
        try {
          c.abort();
        } catch {
          /* ignore */
        }
      }
    };

    // Schedule / reschedule the debounce on enabled+payload+debounceMs changes.
    effect((onCleanup) => {
      const enabled = reg.enabled();
      // Subscribe to payload — any reference change reschedules.
      reg.payload();
      if (!enabled) {
        cancelTimer();
        return;
      }
      cancelTimer();
      timer = setTimeout(() => {
        timer = null;
        runSave();
      }, debounceMs);
      onCleanup(cancelTimer);
    });

    destroyRef.onDestroy(dispose);

    return {
      status: status.asReadonly(),
      flush,
      reset,
      setStatus,
      dispose,
    };
  }
}
