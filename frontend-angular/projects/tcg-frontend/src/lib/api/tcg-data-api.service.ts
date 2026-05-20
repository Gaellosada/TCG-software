import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, map } from 'rxjs';
import { TCG_API_BASE_URL } from './tcg-api.tokens';

export interface TcgInstrumentItem {
  instrument_id?: string;
  symbol?: string;
  display_name?: string;
  [key: string]: unknown;
}

export interface TcgInstrumentList {
  items: TcgInstrumentItem[];
  total: number;
  skip: number;
  limit: number;
}

export interface TcgPriceSeries {
  dates: string[];
  open: number[];
  high: number[];
  low: number[];
  close: number[];
  volume: number[];
}

export interface TcgContinuousOpts {
  strategy?: 'front_month' | string;
  adjustment?: 'none' | 'ratio' | 'difference' | string;
  cycle?: string | null;
  rollOffset?: number;
  start?: string;
  end?: string;
}

export interface TcgInstrumentPricesOpts {
  start?: string;
  end?: string;
  provider?: string;
}

/**
 * Wraps the `/api/data/*` endpoint family. Mirrors React's `api/data.js`:
 *   - `listCollections()` GET `/api/data/collections[?asset_class=]`;
 *   - `listInstruments(collection, {skip, limit})` GET `/api/data/{coll}`;
 *   - `getInstrumentPrices(...)` GET `/api/data/{coll}/{id}[?...]`;
 *   - `getContinuousSeries(...)` GET `/api/data/continuous/{coll}?...`;
 *   - `getAvailableCycles(coll)` GET `/api/data/continuous/{coll}/cycles`.
 *
 * G5: feature-scoped (NOT root). Inject `HttpClient` + `TCG_API_BASE_URL`
 * directly — does not depend on `TcgApiService`.
 */
@Injectable()
export class TcgDataApi {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = inject(TCG_API_BASE_URL);

  listCollections(assetClass?: string): Observable<unknown[]> {
    let params = new HttpParams();
    if (assetClass) params = params.set('asset_class', assetClass);
    return this.http
      .get<{ collections?: unknown[] }>(`${this.baseUrl}/api/data/collections`, { params })
      .pipe(map((res) => res?.collections ?? []));
  }

  listInstruments(
    collection: string,
    opts: { skip?: number; limit?: number } = {},
  ): Observable<TcgInstrumentList> {
    const skip = opts.skip ?? 0;
    const limit = opts.limit ?? 50;
    const params = new HttpParams().set('skip', String(skip)).set('limit', String(limit));
    return this.http.get<TcgInstrumentList>(
      `${this.baseUrl}/api/data/${encodeURIComponent(collection)}`,
      { params },
    );
  }

  getInstrumentPrices(
    collection: string,
    instrumentId: string,
    opts: TcgInstrumentPricesOpts = {},
  ): Observable<TcgPriceSeries> {
    let params = new HttpParams();
    if (opts.start) params = params.set('start', opts.start);
    if (opts.end) params = params.set('end', opts.end);
    if (opts.provider) params = params.set('provider', opts.provider);
    return this.http.get<TcgPriceSeries>(
      `${this.baseUrl}/api/data/${encodeURIComponent(collection)}/${encodeURIComponent(instrumentId)}`,
      { params },
    );
  }

  getContinuousSeries(collection: string, opts: TcgContinuousOpts = {}): Observable<unknown> {
    const strategy = opts.strategy ?? 'front_month';
    const adjustment = opts.adjustment ?? 'none';
    let params = new HttpParams().set('strategy', strategy).set('adjustment', adjustment);
    if (opts.cycle) params = params.set('cycle', opts.cycle);
    if (opts.rollOffset && opts.rollOffset > 0) {
      params = params.set('roll_offset', String(opts.rollOffset));
    }
    if (opts.start) params = params.set('start', opts.start);
    if (opts.end) params = params.set('end', opts.end);
    return this.http.get<unknown>(
      `${this.baseUrl}/api/data/continuous/${encodeURIComponent(collection)}`,
      { params },
    );
  }

  getAvailableCycles(collection: string): Observable<string[]> {
    return this.http
      .get<{ cycles?: string[] }>(
        `${this.baseUrl}/api/data/continuous/${encodeURIComponent(collection)}/cycles`,
      )
      .pipe(map((res) => res?.cycles ?? []));
  }
}
