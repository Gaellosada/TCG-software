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
   * @deprecated Wave I introduces `TcgDataApi.listCollections()` which is
   * the preferred way to fetch collections. `getHealth()` is retained for
   * Wave 0's dev-harness smoke component (`TcgSmokeComponent`) and the
   * new dev-stub backend exposes a dedicated `/api/health` endpoint that
   * this method will switch to once the smoke component is rewired.
   */
  getHealth(): Observable<unknown> {
    return this.http.get<unknown>(`${this.baseUrl}/api/data/collections`);
  }
}
