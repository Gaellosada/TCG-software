import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { TCG_API_BASE_URL } from './tcg-api.tokens';

/**
 * Single root-scoped service for the TCG library — every other library
 * provider must be component- or feature-scoped. The service centralises
 * the host-configurable base URL and exposes typed accessors for the
 * FastAPI backend. Wave 0 only includes a connectivity probe used by the
 * dev-harness smoke; concrete data endpoints are added in later waves.
 */
@Injectable({ providedIn: 'root' })
export class TcgApiService {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = inject(TCG_API_BASE_URL);

  /**
   * Probe the backend to prove wiring works.
   *
   * The TCG FastAPI does not currently expose `/api/health`; we use
   * `/api/data/collections` (a cheap GET that returns a JSON array)
   * because every backend deployment has at least one collection
   * configured. The smoke component only cares that a JSON response
   * comes back, not its shape — so we type the return as `unknown`.
   */
  getHealth(): Observable<unknown> {
    return this.http.get<unknown>(`${this.baseUrl}/api/data/collections`);
  }
}
