import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { TCG_API_BASE_URL } from './tcg-api.tokens';

export type TcgPersistenceCategory = 'RESEARCH' | 'DEV' | 'PROD' | 'ARCHIVE';
export const TCG_PERSISTENCE_CATEGORIES: ReadonlyArray<TcgPersistenceCategory> = [
  'RESEARCH',
  'DEV',
  'PROD',
  'ARCHIVE',
];

export interface TcgSignalOut {
  id: string;
  name: string;
  category: TcgPersistenceCategory;
  inputs?: unknown[];
  rules?: unknown;
  settings?: unknown;
  description?: string;
  [key: string]: unknown;
}

export interface TcgSignalCreatePayload {
  id: string;
  name: string;
  category: TcgPersistenceCategory;
  inputs?: unknown[];
  rules?: unknown;
  settings?: unknown;
  description?: string;
}

export interface TcgPortfolioOut {
  id: string;
  name: string;
  category: TcgPersistenceCategory;
  legs?: unknown[];
  rebalance?: string;
  [key: string]: unknown;
}

export interface TcgPortfolioCreatePayload {
  id: string;
  name: string;
  category: TcgPersistenceCategory;
  legs?: unknown[];
  rebalance?: string;
}

export interface TcgBasketOut {
  id: string;
  name: string;
  category: TcgPersistenceCategory;
  asset_class: 'equity' | 'index' | 'future' | 'option';
  legs?: Array<{ instrument: unknown; weight: number }>;
  [key: string]: unknown;
}

export interface TcgBasketCreatePayload {
  id: string;
  name: string;
  category: TcgPersistenceCategory;
  asset_class: 'equity' | 'index' | 'future' | 'option';
  legs?: Array<{ instrument: unknown; weight: number }>;
}

/**
 * Categorise a persistence error into a human-readable label. Mirrors the
 * React `describePersistenceError` helper. Status codes 409 / 413 / 422
 * carry first-class messaging because they show up in the autosave flow.
 */
export function describePersistenceError(err: unknown): string {
  if (!err) return 'Unknown error';
  if (err instanceof HttpErrorResponse) {
    if (err.error && typeof err.error === 'object') {
      const e = err.error as { detail?: string; message?: string };
      const msg = e.detail || e.message || err.statusText || 'Request failed';
      const status = err.status;
      if (status === 409) return `Conflict (409): ${msg}`;
      if (status === 413) return `Payload too large (413): ${msg}`;
      if (status === 422) return `Validation error (422): ${msg}`;
      if (status >= 400 && status < 500) return `Client error (${status}): ${msg}`;
      if (status >= 500) return `Server error (${status}): ${msg}`;
      return msg;
    }
    return err.message || String(err);
  }
  if (err instanceof Error) {
    if (err.name === 'AbortError') return 'Cancelled';
    return err.message;
  }
  return String(err);
}

/**
 * Wraps the `/api/persistence/*` CRUD endpoints. Mirrors React's
 * `api/persistence.js` 1:1 — signals, portfolios, baskets. Categories are
 * the locked enum (`RESEARCH | DEV | PROD | ARCHIVE`).
 *
 * G5: feature-scoped.
 */
@Injectable()
export class TcgPersistenceApi {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = inject(TCG_API_BASE_URL);

  private url(path: string): string {
    return `${this.baseUrl}/api/persistence${path}`;
  }

  // ── Signals ────────────────────────────────────────────────────────
  createSignal(payload: TcgSignalCreatePayload): Observable<TcgSignalOut> {
    return this.http.post<TcgSignalOut>(this.url('/signals'), payload);
  }
  listSignals(category: TcgPersistenceCategory): Observable<TcgSignalOut[]> {
    return this.http.get<TcgSignalOut[]>(
      this.url(`/signals?category=${encodeURIComponent(category)}`),
    );
  }
  getSignal(id: string): Observable<TcgSignalOut> {
    return this.http.get<TcgSignalOut>(this.url(`/signals/${encodeURIComponent(id)}`));
  }
  updateSignal(id: string, payload: Omit<TcgSignalCreatePayload, 'id'>): Observable<TcgSignalOut> {
    return this.http.put<TcgSignalOut>(this.url(`/signals/${encodeURIComponent(id)}`), payload);
  }
  archiveSignal(id: string): Observable<void> {
    return this.http.delete<void>(this.url(`/signals/${encodeURIComponent(id)}`));
  }

  // ── Portfolios ─────────────────────────────────────────────────────
  createPortfolio(payload: TcgPortfolioCreatePayload): Observable<TcgPortfolioOut> {
    return this.http.post<TcgPortfolioOut>(this.url('/portfolios'), payload);
  }
  listPortfolios(category: TcgPersistenceCategory): Observable<TcgPortfolioOut[]> {
    return this.http.get<TcgPortfolioOut[]>(
      this.url(`/portfolios?category=${encodeURIComponent(category)}`),
    );
  }
  getPortfolio(id: string): Observable<TcgPortfolioOut> {
    return this.http.get<TcgPortfolioOut>(this.url(`/portfolios/${encodeURIComponent(id)}`));
  }
  updatePortfolio(
    id: string,
    payload: Omit<TcgPortfolioCreatePayload, 'id'>,
  ): Observable<TcgPortfolioOut> {
    return this.http.put<TcgPortfolioOut>(
      this.url(`/portfolios/${encodeURIComponent(id)}`),
      payload,
    );
  }
  archivePortfolio(id: string): Observable<void> {
    return this.http.delete<void>(this.url(`/portfolios/${encodeURIComponent(id)}`));
  }

  // ── Baskets ────────────────────────────────────────────────────────
  createBasket(payload: TcgBasketCreatePayload): Observable<TcgBasketOut> {
    return this.http.post<TcgBasketOut>(this.url('/baskets'), payload);
  }
  listBaskets(category: TcgPersistenceCategory): Observable<TcgBasketOut[]> {
    return this.http.get<TcgBasketOut[]>(
      this.url(`/baskets?category=${encodeURIComponent(category)}`),
    );
  }
  getBasket(id: string): Observable<TcgBasketOut> {
    return this.http.get<TcgBasketOut>(this.url(`/baskets/${encodeURIComponent(id)}`));
  }
  updateBasket(
    id: string,
    payload: Omit<TcgBasketCreatePayload, 'id'>,
  ): Observable<TcgBasketOut> {
    return this.http.put<TcgBasketOut>(this.url(`/baskets/${encodeURIComponent(id)}`), payload);
  }
  archiveBasket(id: string): Observable<void> {
    return this.http.delete<void>(this.url(`/baskets/${encodeURIComponent(id)}`));
  }
}
