import {
  DestroyRef,
  Signal,
  computed,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { Subscription } from 'rxjs';
import {
  TcgChainResponse,
  TcgOptionChainParams,
  TcgOptionsApi,
} from '../../api/tcg-options-api.service';
import { tcgAddDays, tcgTodayIso } from './data-format';

/**
 * Filter snapshot owned by the chain hook. Mirrors React's
 * `useOptionsChain` filter shape.
 */
export interface TcgOptionChainFilters {
  root: string | null;
  date: string | null;
  type: 'C' | 'P' | 'both';
  expirationMin: string | null;
  expirationMax: string | null;
  strikeMin: number | null;
  strikeMax: number | null;
  /**
   * Decision C: transient local state — never persisted. Default flipped
   * to `true` so OPT_VIX (no stored greeks) renders greeks without an
   * extra click.
   */
  computeMissing: boolean;
  expirationCycle: string | null;
}

export interface TcgOptionChainInitialFilters {
  date?: string | null;
  type?: 'C' | 'P' | 'both';
  expirationMin?: string | null;
  expirationMax?: string | null;
  strikeMin?: number | null;
  strikeMax?: number | null;
  computeMissing?: boolean;
  expirationCycle?: string | null;
}

function buildDefaultFilters(
  initialRoot: string | null,
  overrides: TcgOptionChainInitialFilters = {},
): TcgOptionChainFilters {
  const anchor = overrides.date ?? tcgTodayIso();
  return {
    root: initialRoot,
    date: anchor,
    type: (overrides.type as 'C' | 'P' | 'both' | undefined) ?? 'both',
    expirationMin: overrides.expirationMin ?? anchor,
    expirationMax: overrides.expirationMax ?? tcgAddDays(anchor, 90),
    strikeMin: overrides.strikeMin ?? null,
    strikeMax: overrides.strikeMax ?? null,
    computeMissing: overrides.computeMissing ?? true,
    expirationCycle: overrides.expirationCycle ?? null,
  };
}

export type TcgOptionChainState = TcgChainResponse | { error: Error } | null;

export interface TcgOptionsChainResource {
  readonly filters: Signal<TcgOptionChainFilters>;
  readonly chainData: Signal<TcgOptionChainState>;
  readonly loading: Signal<boolean>;
  /** Fetches a chain for the current `filters` snapshot. */
  fetchChain(): Promise<void>;
  /** Merges partial filter changes into the current snapshot. */
  updateFilters(partial: Partial<TcgOptionChainFilters>): void;
  /** Aborts any in-flight request. */
  abort(): void;
}

/**
 * Reactive port of React's `useOptionsChain` hook.
 *
 * Owns the filter snapshot + last chain response. Callers drive fetches
 * explicitly (no implicit fetch-on-update) — typically debounced via an
 * `effect()` on the filters signal in the consuming component.
 *
 * Abort safety: a fresh `fetchChain()` while a previous request is in
 * flight cancels the earlier one. Component teardown also cancels.
 */
export function tcgUseOptionsChain(
  api: TcgOptionsApi,
  initialRoot: string | null = null,
  initialFilters: TcgOptionChainInitialFilters = {},
): TcgOptionsChainResource {
  const filters = signal<TcgOptionChainFilters>(
    buildDefaultFilters(initialRoot, initialFilters),
  );
  const chainData = signal<TcgOptionChainState>(null);
  const loading = signal(false);
  let sub: Subscription | null = null;

  const destroyRef = inject(DestroyRef);
  destroyRef.onDestroy(() => {
    if (sub) sub.unsubscribe();
  });

  function abort(): void {
    if (sub) {
      sub.unsubscribe();
      sub = null;
    }
    loading.set(false);
  }

  async function fetchChain(): Promise<void> {
    const f = untracked(filters);
    if (!f.root || !f.date || !f.expirationMin || !f.expirationMax) return;
    if (sub) sub.unsubscribe();
    loading.set(true);
    const params: TcgOptionChainParams = {
      date: f.date,
      type: f.type,
      expirationMin: f.expirationMin,
      expirationMax: f.expirationMax,
      strikeMin: f.strikeMin,
      strikeMax: f.strikeMax,
      computeMissing: f.computeMissing,
      expirationCycle: f.expirationCycle,
    };
    return new Promise<void>((resolve) => {
      sub = api.getOptionChain(f.root!, params).subscribe({
        next: (res) => {
          chainData.set(res);
        },
        error: (err: unknown) => {
          chainData.set({ error: err instanceof Error ? err : new Error(String(err)) });
          loading.set(false);
          resolve();
        },
        complete: () => {
          loading.set(false);
          resolve();
        },
      });
    });
  }

  function updateFilters(partial: Partial<TcgOptionChainFilters>): void {
    filters.update((prev) => ({ ...prev, ...partial }));
  }

  return {
    filters: filters.asReadonly(),
    chainData: chainData.asReadonly(),
    loading: loading.asReadonly(),
    fetchChain,
    updateFilters,
    abort,
  };
}

/** Computed convenience — `chainData` carries a non-null `rows` array. */
export function tcgChainRows(
  chainData: Signal<TcgOptionChainState>,
): Signal<unknown[] | null> {
  return computed(() => {
    const d = chainData();
    if (!d) return null;
    if ('error' in d && d.error) return null;
    const rows = (d as TcgChainResponse).rows;
    return Array.isArray(rows) ? rows : null;
  });
}
