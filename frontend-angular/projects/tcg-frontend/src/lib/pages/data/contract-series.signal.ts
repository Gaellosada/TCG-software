import {
  DestroyRef,
  Signal,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { Subscription } from 'rxjs';
import {
  TcgContractSeries,
  TcgOptionsApi,
} from '../../api/tcg-options-api.service';

/**
 * Reactive port of React's `useContractSeries` hook.
 *
 * Fetches `/api/options/contract/{collection}/{id}` whenever any of the
 * input signals change. Each new input value cancels the in-flight
 * request (subscription-teardown maps to AbortSignal cancellation under
 * Angular's `HttpClient`).
 *
 * Decision C (preserved verbatim from React): `computeMissing` defaults
 * to TRUE. Stored-greek collections short-circuit per row; flipping to
 * false here would regress OPT_VIX (no stored greeks at CBOE).
 */
export interface TcgContractSeriesResource {
  readonly data: Signal<TcgContractSeries | null>;
  readonly loading: Signal<boolean>;
  readonly error: Signal<Error | null>;
}

export interface TcgContractSeriesInputs {
  collection: Signal<string | null>;
  contractId: Signal<string | null>;
  /** Defaults to `true` — preserved from React. */
  computeMissing?: Signal<boolean>;
  dateFrom?: Signal<string | null>;
  dateTo?: Signal<string | null>;
}

export function tcgUseContractSeries(
  inputs: TcgContractSeriesInputs,
  api: TcgOptionsApi,
): TcgContractSeriesResource {
  const data = signal<TcgContractSeries | null>(null);
  const loading = signal(false);
  const error = signal<Error | null>(null);

  let sub: Subscription | null = null;

  const destroyRef = inject(DestroyRef);
  destroyRef.onDestroy(() => {
    if (sub) sub.unsubscribe();
  });

  effect(() => {
    const collection = inputs.collection();
    const contractId = inputs.contractId();
    const computeMissing = inputs.computeMissing ? inputs.computeMissing() : true;
    const dateFrom = inputs.dateFrom ? inputs.dateFrom() : null;
    const dateTo = inputs.dateTo ? inputs.dateTo() : null;

    if (sub) {
      sub.unsubscribe();
      sub = null;
    }

    if (!collection || !contractId) {
      untracked(() => {
        data.set(null);
        loading.set(false);
        error.set(null);
      });
      return;
    }

    untracked(() => {
      loading.set(true);
      error.set(null);
    });
    sub = api
      .getOptionContract(collection, contractId, { computeMissing, dateFrom, dateTo })
      .subscribe({
        next: (res) => {
          untracked(() => {
            data.set(res);
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
    data: data.asReadonly(),
    loading: loading.asReadonly(),
    error: error.asReadonly(),
  };
}
