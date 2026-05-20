import { Injectable, inject } from '@angular/core';
import { Observable, defer, map, shareReplay, throwError } from 'rxjs';
import { TcgDataApi } from './tcg-data-api.service';

/**
 * Reference to an instrument the summary helper can resolve.
 */
export interface TcgSeriesRef {
  collection: string;
  instrument_id: string;
}

/**
 * Short summary of an instrument's price series — length + date span.
 *
 * Mirrors React's `api/seriesSummary.js`. `start` / `end` are normalised
 * to ISO `YYYY-MM-DD` strings even when the backend ships dates as
 * YYYYMMDD integers (defensive branch).
 */
export interface TcgSeriesSummary {
  collection: string;
  instrument_id: string;
  length: number;
  start: string | null;
  end: string | null;
}

/**
 * Tiny helper that wraps `TcgDataApi.getInstrumentPrices` with an in-service
 * `Map` cache keyed on `${collection}:${instrument_id}`. Repeated lookups for
 * the same ref return the cached observable so subsequent toggles of any
 * "details" UI don't re-fetch.
 *
 * G5: feature-scoped (provide via the consuming page's `providers: [...]`).
 *
 * Cache eviction: failed observables are evicted (the next subscribe re-runs
 * the underlying fetch). Successful observables stay cached for the service's
 * lifetime; recreating the service (new feature scope) discards the cache.
 */
@Injectable()
export class TcgSeriesSummaryApi {
  private readonly data = inject(TcgDataApi);
  private readonly cache = new Map<string, Observable<TcgSeriesSummary>>();

  private static key(ref: TcgSeriesRef): string {
    return `${ref.collection}:${ref.instrument_id}`;
  }

  private static toIsoDate(value: unknown): string {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' && Number.isFinite(value)) {
      const n = Math.trunc(value);
      const y = Math.floor(n / 10000);
      const m = Math.floor((n % 10000) / 100);
      const d = n % 100;
      const mm = String(m).padStart(2, '0');
      const dd = String(d).padStart(2, '0');
      return `${y}-${mm}-${dd}`;
    }
    return String(value);
  }

  getSeriesSummary(ref: TcgSeriesRef): Observable<TcgSeriesSummary> {
    if (!ref || !ref.collection || !ref.instrument_id) {
      return throwError(() => new Error('Invalid series reference'));
    }
    const k = TcgSeriesSummaryApi.key(ref);
    const cached = this.cache.get(k);
    if (cached) return cached;

    const obs = defer(() => this.data.getInstrumentPrices(ref.collection, ref.instrument_id))
      .pipe(
        map((res) => {
          const dates = (res && Array.isArray(res.dates) ? res.dates : []) as unknown[];
          const length = dates.length;
          const start =
            length > 0 ? TcgSeriesSummaryApi.toIsoDate(dates[0]) : null;
          const end =
            length > 0 ? TcgSeriesSummaryApi.toIsoDate(dates[length - 1]) : null;
          return {
            collection: ref.collection,
            instrument_id: ref.instrument_id,
            length,
            start,
            end,
          };
        }),
        shareReplay({ bufferSize: 1, refCount: false }),
      );
    // Wrap so errors evict from the cache for retry. Use defer + from to
    // ensure each subscribe sees the same shared observable but evicts
    // on error.
    const wrapped: Observable<TcgSeriesSummary> = new Observable((subscriber) => {
      const sub = obs.subscribe({
        next: (v) => subscriber.next(v),
        error: (err) => {
          this.cache.delete(k);
          subscriber.error(err);
        },
        complete: () => subscriber.complete(),
      });
      return () => sub.unsubscribe();
    });
    this.cache.set(k, wrapped);
    return wrapped;
  }

  /** Test helper — evicts the entire cache. */
  resetCache(): void {
    this.cache.clear();
  }
}
