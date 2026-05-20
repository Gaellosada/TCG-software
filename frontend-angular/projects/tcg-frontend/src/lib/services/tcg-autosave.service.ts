import { DestroyRef, Injectable, Signal, effect, inject, untracked } from '@angular/core';

export interface TcgAutosaveRegistration<T> {
  enabled: Signal<boolean>;
  dirty: Signal<boolean>;
  value: Signal<T>;
  onSave: (value: T) => void;
  debounceMs?: number;
}

const DEFAULT_DEBOUNCE_MS = 500;

/**
 * Debounced autosave — the simpler cousin of `TcgBackendAutosaveService`.
 * Mirrors React's `useAutosave.js`:
 *   - schedules `onSave(value)` after `debounceMs` of inactivity when
 *     both `enabled` and `dirty` are true;
 *   - cancels the timer (without firing) when `enabled` flips false or
 *     the host destroys;
 *   - installs `beforeunload` + `pagehide` listeners while enabled that
 *     synchronously flush any pending payload before the page tears down.
 *
 * Component-scoped (G5).
 */
@Injectable()
export class TcgAutosaveService {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pendingValue: unknown = null;
  private hasPending = false;

  register<T>(reg: TcgAutosaveRegistration<T>): void {
    const destroyRef = inject(DestroyRef);
    const debounceMs = reg.debounceMs ?? DEFAULT_DEBOUNCE_MS;

    // Watch [enabled, dirty, value] — when both flags are true, (re)schedule.
    effect((onCleanup) => {
      const enabled = reg.enabled();
      const dirty = reg.dirty();
      const value = reg.value();

      if (!enabled || !dirty) {
        if (this.timer) {
          clearTimeout(this.timer);
          this.timer = null;
        }
        return;
      }

      this.pendingValue = value;
      this.hasPending = true;
      if (this.timer) clearTimeout(this.timer);
      this.timer = setTimeout(() => {
        const snapshot = this.pendingValue;
        const hadPending = this.hasPending;
        this.timer = null;
        this.pendingValue = null;
        this.hasPending = false;
        if (hadPending) {
          // Read `onSave` via untracked() so we don't subscribe to it.
          untracked(() => reg.onSave(snapshot as T));
        }
      }, debounceMs);

      onCleanup(() => {
        if (this.timer) {
          clearTimeout(this.timer);
          this.timer = null;
        }
      });
    });

    // Install flush-on-unload listeners while enabled.
    const flush = (): void => {
      if (this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
      }
      if (!this.hasPending) return;
      const snapshot = this.pendingValue;
      this.pendingValue = null;
      this.hasPending = false;
      try {
        reg.onSave(snapshot as T);
      } catch {
        /* swallow — we're unloading */
      }
    };

    effect((onCleanup) => {
      if (!reg.enabled()) return;
      window.addEventListener('beforeunload', flush);
      window.addEventListener('pagehide', flush);
      onCleanup(() => {
        window.removeEventListener('beforeunload', flush);
        window.removeEventListener('pagehide', flush);
      });
    });

    destroyRef.onDestroy(() => {
      if (this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
      }
      this.pendingValue = null;
      this.hasPending = false;
    });
  }
}
