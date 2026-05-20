import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';

import { TCG_API_BASE_URL } from '@tcg/frontend';

import { routes } from './app.routes';

/**
 * Dev-harness providers. The harness is the host stand-in: it is allowed
 * to call `provideRouter` / `provideHttpClient` / `provideZoneChangeDetection`
 * because real hosts will also configure those. The library itself never
 * does so.
 *
 * `TCG_API_BASE_URL` is the only token a real host must supply for the
 * library to function — here we point at the local FastAPI dev server.
 * Override via environment in a real host build.
 */
export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes),
    provideHttpClient(),
    { provide: TCG_API_BASE_URL, useValue: 'http://localhost:8000' },
  ],
};
