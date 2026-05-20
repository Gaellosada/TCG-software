import { InjectionToken } from '@angular/core';

/**
 * Host-configurable base URL for the TCG FastAPI backend.
 *
 * The library never hardcodes the API host. Hosts must provide this token
 * at bootstrap, typically pointing at `http://localhost:8000` in dev or a
 * same-origin empty string in production behind a reverse proxy.
 *
 * Example:
 *   providers: [{ provide: TCG_API_BASE_URL, useValue: 'http://localhost:8000' }]
 */
export const TCG_API_BASE_URL = new InjectionToken<string>('TCG_API_BASE_URL');
