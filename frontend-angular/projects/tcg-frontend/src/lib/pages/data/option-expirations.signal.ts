import {
  DestroyRef,
  Signal,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { Subscription } from 'rxjs';
import { TcgOptionsApi } from '../../api/tcg-options-api.service';

/**
 * Reactive port of React's `useOptionExpirations` hook.
 *
 * Re-fetches whenever `root()` changes; null root resolves to an empty
 * list. Returns three signals matching the React hook's `{expirations,
 * loading, error}` shape — but as signals not as a plain object.
 *
 * Cancellation: each new `root()` value unsubscribes from any in-flight
 * request; component teardown unsubscribes too via `DestroyRef`.
 */
export interface TcgOptionExpirationsResource {
  readonly expirations: Signal<string[]>;
  readonly loading: Signal<boolean>;
  readonly error: Signal<Error | null>;
}

export function tcgUseOptionExpirations(
  rootSignal: Signal<string | null>,
  api: TcgOptionsApi,
): TcgOptionExpirationsResource {
  const expirations = signal<string[]>([]);
  const loading = signal(false);
  const error = signal<Error | null>(null);
  let sub: Subscription | null = null;

  const destroyRef = inject(DestroyRef);
  destroyRef.onDestroy(() => {
    if (sub) sub.unsubscribe();
  });

  effect(() => {
    const root = rootSignal();
    // Cancel any prior fetch on input change.
    if (sub) {
      sub.unsubscribe();
      sub = null;
    }
    if (!root) {
      untracked(() => {
        expirations.set([]);
        loading.set(false);
        error.set(null);
      });
      return;
    }
    untracked(() => {
      loading.set(true);
      error.set(null);
    });
    sub = api.getOptionExpirations(root).subscribe({
      next: (res) => {
        untracked(() => {
          expirations.set(Array.isArray(res?.expirations) ? res.expirations : []);
          loading.set(false);
        });
      },
      error: (err: unknown) => {
        untracked(() => {
          error.set(err instanceof Error ? err : new Error(String(err)));
          loading.set(false);
        });
      },
    });
  });

  return {
    expirations: expirations.asReadonly(),
    loading: loading.asReadonly(),
    error: error.asReadonly(),
  };
}

/** Convenience: latest expiration on top of the list. */
export function tcgReversedExpirations(src: Signal<string[]>): Signal<string[]> {
  return computed(() => [...src()].reverse());
}
