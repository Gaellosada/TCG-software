import { Routes } from '@angular/router';

import { TcgSmokeComponent, tcgRoutes } from '@tcg/frontend';

/**
 * Dev-harness routes. The library's `tcgRoutes` is spread inside the
 * `''` path so the host stand-in mounts every TCG page under `/` —
 * `/data`, `/settings`, `/help`, etc.
 *
 * `/smoke` keeps the Wave 0 connectivity probe visible so the
 * dev-stub-backend (and any real FastAPI) is easy to verify.
 */
export const routes: Routes = [
  { path: 'smoke', component: TcgSmokeComponent },
  ...tcgRoutes,
];
