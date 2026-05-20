import { DestroyRef, Injectable, inject, signal } from '@angular/core';

/**
 * Component-scoped helper that mirrors React's `useAbortableAction` hook.
 * Provide it via `providers: [TcgAbortableActionService]` on the component
 * that owns the run-button-style action (Indicators, Signals, Portfolio).
 *
 * `run(fn)` aborts any in-flight controller, creates a new one, invokes
 * `fn({signal})` and toggles `running` around the call. `abort()` cancels
 * the current controller and clears `running`. The service auto-aborts on
 * the host's `DestroyRef.onDestroy(...)`.
 */
@Injectable()
export class TcgAbortableActionService {
  readonly running = signal(false);

  private controller: AbortController | null = null;

  constructor() {
    const destroyRef = inject(DestroyRef);
    destroyRef.onDestroy(() => {
      if (this.controller) this.controller.abort();
      this.controller = null;
    });
  }

  abort(): void {
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
      this.running.set(false);
    }
  }

  async run<T>(fn: (opts: { signal: AbortSignal }) => Promise<T>): Promise<T> {
    if (this.controller) this.controller.abort();
    const controller = new AbortController();
    this.controller = controller;
    this.running.set(true);
    try {
      return await fn({ signal: controller.signal });
    } finally {
      // Only flip `running` off when this specific call wasn't aborted —
      // if the user hit Run again mid-flight, the new invocation already
      // set running=true; clearing it here would falsely toggle the UI off.
      if (!controller.signal.aborted) this.running.set(false);
      if (this.controller === controller) this.controller = null;
    }
  }
}
