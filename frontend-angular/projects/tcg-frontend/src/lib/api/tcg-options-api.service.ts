import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { TCG_API_BASE_URL } from './tcg-api.tokens';

/**
 * Per-root descriptor returned by `/api/options/roots`. The wire shape is
 * loose — newer backends ship `stored_greeks_ratio` + `has_computed_greeks`,
 * older builds only carry `has_greeks`. The Angular code reads both.
 */
export interface TcgOptionRootInfo {
  name: string;
  collection?: string;
  has_greeks?: boolean;
  stored_greeks_ratio?: number;
  has_computed_greeks?: boolean;
  last_trade_date?: string | null;
  expiration_last?: string | null;
  [key: string]: unknown;
}

export interface TcgOptionRootsResponse {
  roots: TcgOptionRootInfo[];
}

export interface TcgOptionExpirationsResponse {
  root: string;
  expirations: string[];
}

export interface TcgOptionChainParams {
  date: string;
  type?: 'C' | 'P' | 'both' | string;
  expirationMin: string;
  expirationMax: string;
  strikeMin?: number | null;
  strikeMax?: number | null;
  computeMissing?: boolean | null;
  expirationCycle?: string | null;
}

/**
 * Wire shape of a single ComputeResult-wrapped Greek/IV cell.
 * Source values: 'stored' | 'computed' | 'missing'.
 */
export interface TcgComputeResult {
  value: number | null;
  source: 'stored' | 'computed' | 'missing' | string;
  model?: string;
  inputs_used?: {
    underlying_price?: number | null;
    iv?: number | null;
    ttm?: number | null;
    r?: number | null;
    [key: string]: unknown;
  };
  error_code?: string;
  error_detail?: string;
  [key: string]: unknown;
}

export interface TcgChainRow {
  contract_id: string;
  expiration: string;
  expiration_cycle?: string;
  strike: number;
  type: 'C' | 'P';
  bid?: number | null;
  mid?: number | null;
  ask?: number | null;
  open_interest?: number | null;
  iv?: TcgComputeResult;
  delta?: TcgComputeResult;
  gamma?: TcgComputeResult;
  theta?: TcgComputeResult;
  vega?: TcgComputeResult;
  [key: string]: unknown;
}

export interface TcgChainResponse {
  rows?: TcgChainRow[];
  date?: string;
  underlying_price?: { value: number | null; [key: string]: unknown };
  error?: { message?: string; [key: string]: unknown };
  [key: string]: unknown;
}

export interface TcgContractRow {
  date: string;
  mid?: number | null;
  volume?: number | null;
  underlying_price_stored?: number | null;
  delta_stored?: number | null;
  iv?: TcgComputeResult;
  delta?: TcgComputeResult;
  gamma?: TcgComputeResult;
  theta?: TcgComputeResult;
  vega?: TcgComputeResult;
  [key: string]: unknown;
}

export interface TcgOptionContractMeta {
  contract_id: string;
  strike?: number;
  type?: 'C' | 'P';
  expiration?: string;
  expiration_cycle?: string;
  root_underlying?: string;
  provider?: string;
  [key: string]: unknown;
}

export interface TcgContractSeries {
  contract: TcgOptionContractMeta;
  rows: TcgContractRow[];
  [key: string]: unknown;
}

export interface TcgContractOpts {
  computeMissing?: boolean | null;
  dateFrom?: string | null;
  dateTo?: string | null;
}

export interface TcgChainSnapshotPoint {
  strike: number;
  K_over_S?: number;
  expiration_cycle?: string;
  value?: { value: number | null; [key: string]: unknown };
  [key: string]: unknown;
}

export interface TcgChainSnapshotSeries {
  expiration?: string;
  points: TcgChainSnapshotPoint[];
  [key: string]: unknown;
}

export interface TcgChainSnapshotResponse {
  series: TcgChainSnapshotSeries[];
  underlying_price?: { value: number | null; [key: string]: unknown };
  [key: string]: unknown;
}

export interface TcgChainSnapshotOpts {
  date: string;
  type?: 'C' | 'P';
  expirations: string[];
  field?: 'iv' | 'delta' | string;
  expiration_cycle?: string | null;
}

/**
 * Wraps the `/api/options/*` endpoint family. Mirrors React's `api/options.js`:
 *   - `getOptionRoots()`     GET `/api/options/roots`
 *   - `getOptionExpirations(root)` GET `/api/options/expirations?root=`
 *   - `getOptionChain(root, params)` GET `/api/options/chain?...`
 *   - `getOptionContract(coll, id, opts)` GET `/api/options/contract/{coll}/{id}[?...]`
 *   - `getChainSnapshot(root, opts)` GET `/api/options/chain-snapshot?...`
 *     (note: `expirations` is repeated via `HttpParams.append`)
 *   - `selectOption(query)`  GET `/api/options/select?q=<JSON>`
 *
 * G5: feature-scoped (NOT root). Inject `HttpClient` + `TCG_API_BASE_URL`
 * directly — does not depend on `TcgApiService`.
 *
 * G6: every URL is composed from the injected `TCG_API_BASE_URL`.
 *
 * Cancellation: callers compose with `takeUntil(destroy$)` (or `firstValueFrom`
 * + abort handling) rather than passing an `AbortSignal`. The React side
 * passed signals into `fetch()`; in Angular, subscription teardown is the
 * canonical mechanism.
 */
@Injectable()
export class TcgOptionsApi {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = inject(TCG_API_BASE_URL);

  getOptionRoots(): Observable<TcgOptionRootsResponse> {
    return this.http.get<TcgOptionRootsResponse>(`${this.baseUrl}/api/options/roots`);
  }

  getOptionExpirations(root: string): Observable<TcgOptionExpirationsResponse> {
    const params = new HttpParams().set('root', String(root));
    return this.http.get<TcgOptionExpirationsResponse>(
      `${this.baseUrl}/api/options/expirations`,
      { params },
    );
  }

  getOptionChain(root: string, params: TcgOptionChainParams): Observable<TcgChainResponse> {
    let qp = new HttpParams().set('root', String(root)).set('date', String(params.date));
    if (params.type != null) qp = qp.set('type', String(params.type));
    qp = qp.set('expiration_min', String(params.expirationMin));
    qp = qp.set('expiration_max', String(params.expirationMax));
    if (params.strikeMin != null) qp = qp.set('strike_min', String(params.strikeMin));
    if (params.strikeMax != null) qp = qp.set('strike_max', String(params.strikeMax));
    if (params.computeMissing != null) {
      qp = qp.set('compute_missing', String(params.computeMissing));
    }
    if (params.expirationCycle != null && String(params.expirationCycle).trim() !== '') {
      qp = qp.set('expiration_cycle', String(params.expirationCycle));
    }
    return this.http.get<TcgChainResponse>(`${this.baseUrl}/api/options/chain`, { params: qp });
  }

  getOptionContract(
    collection: string,
    contractId: string,
    opts: TcgContractOpts = {},
  ): Observable<TcgContractSeries> {
    let qp = new HttpParams();
    if (opts.computeMissing != null) qp = qp.set('compute_missing', String(opts.computeMissing));
    if (opts.dateFrom != null) qp = qp.set('date_from', String(opts.dateFrom));
    if (opts.dateTo != null) qp = qp.set('date_to', String(opts.dateTo));
    return this.http.get<TcgContractSeries>(
      `${this.baseUrl}/api/options/contract/${encodeURIComponent(collection)}/${encodeURIComponent(contractId)}`,
      { params: qp },
    );
  }

  /**
   * Multi-expiration smile snapshot. `expirations` is serialised as
   * repeated query params (e.g. `?expirations=2024-04-19&expirations=2024-05-17`)
   * — `HttpParams.append`, NOT `.set`, preserves duplicates.
   */
  getChainSnapshot(
    root: string,
    opts: TcgChainSnapshotOpts,
  ): Observable<TcgChainSnapshotResponse> {
    let qp = new HttpParams().set('root', String(root)).set('date', String(opts.date));
    if (opts.type != null) qp = qp.set('type', String(opts.type));
    if (Array.isArray(opts.expirations)) {
      for (const exp of opts.expirations) {
        qp = qp.append('expirations', String(exp));
      }
    }
    if (opts.field != null) qp = qp.set('field', String(opts.field));
    if (opts.expiration_cycle != null) {
      qp = qp.set('expiration_cycle', String(opts.expiration_cycle));
    }
    return this.http.get<TcgChainSnapshotResponse>(
      `${this.baseUrl}/api/options/chain-snapshot`,
      { params: qp },
    );
  }

  /**
   * Option selection resolver. The entire `selectQuery` object is
   * JSON-stringified and URL-encoded — backend uses
   * `SelectQuery.model_validate_json` to parse.
   */
  selectOption(selectQuery: Record<string, unknown>): Observable<unknown> {
    const params = new HttpParams().set('q', JSON.stringify(selectQuery));
    return this.http.get<unknown>(`${this.baseUrl}/api/options/select`, { params });
  }

  /**
   * Resolve one or more option streams between two dates. POST endpoint;
   * the backend may stream progress on `/api/options/stream/progress/{taskId}`.
   *
   * Mirrors React's `resolveOptionStream(streams, start, end, {signal, onProgress})`.
   * Progress polling is the caller's responsibility — call `pollStreamProgress`
   * on a timer until the Observable completes.
   */
  resolveOptionStream(
    streams: Array<{ ref: Record<string, unknown>; label: string }>,
    start: string,
    end: string,
    taskId?: string,
  ): Observable<unknown> {
    const body = {
      streams,
      start,
      end,
      task_id: taskId,
    };
    return this.http.post<unknown>(`${this.baseUrl}/api/options/stream`, body);
  }

  /** Poll progress for an in-flight `resolveOptionStream` task. */
  pollStreamProgress(
    taskId: string,
  ): Observable<{ done: number; total: number; fraction: number }> {
    return this.http.get<{ done: number; total: number; fraction: number }>(
      `${this.baseUrl}/api/options/stream/progress/${encodeURIComponent(taskId)}`,
    );
  }
}
