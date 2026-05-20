# @tcg/frontend — TCG Angular feature library

A standalone-component-based Angular feature library that hosts consume from
source via `public-api.ts`. Not published to npm — pull this fork as a git
submodule/subtree and add a TypeScript path alias.

## Integration shape

This library is consumed as **source**, not as a built artifact. The host's
`tsconfig.json` points `@tcg/frontend` at `src/public-api.ts`:

```json
{
  "compilerOptions": {
    "paths": {
      "@tcg/frontend": [
        "./vendor/tcg-software-angular/frontend-angular/projects/tcg-frontend/src/public-api.ts"
      ]
    }
  }
}
```

No `ng build tcg-frontend` is required in the host's dev loop — TypeScript
walks the source directly.

## Host bootstrap recipe

```ts
import { bootstrapApplication } from '@angular/platform-browser';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';

import {
  TCG_API_BASE_URL,
  tcgRoutes,
} from '@tcg/frontend';

bootstrapApplication(AppComponent, {
  providers: [
    provideHttpClient(),
    provideRouter([
      ...tcgRoutes,
      // host's own routes here
    ]),
    { provide: TCG_API_BASE_URL, useValue: '' /* same-origin */ },
  ],
});
```

`TCG_API_BASE_URL` is the only token a host MUST provide. The library uses it
for every backend call; never hardcodes URLs.

## Public surface (consumed via `public-api.ts`)

### Core
- `TcgApiService` — root-scoped HTTP entry point (`getHealth()` smoke probe)
- `TCG_API_BASE_URL` — injection token for the host-configurable backend URL
- `tcgRoutes` — `Routes` array consumers spread into their own router config

### Components (standalone, selectors `tcg-*`)
- `TcgChartComponent` — Plotly wrapper with lazy-loaded Plotly + CSV export
- `TcgConfirmDialogComponent` — CDK-overlay modal confirmation
- `TcgSaveStatusComponent` — `idle|saving|saved|error` indicator
- `TcgSaveControlsComponent` — Save button + Auto save checkbox
- `TcgSidebarComponent` — collapsible router navigation
- `TcgPageContainerComponent` — page-shell wrapper
- `TcgCardComponent`, `TcgIconComponent`, `TcgInlineNameInputComponent`,
  `TcgPillToggleComponent`, `TcgPlaceholderPageComponent`,
  `TcgRfrInputComponent`, `TcgErrorBoundaryComponent`,
  `TcgErrorCardComponent`
- `TcgInstrumentPickerModalComponent` (+ 7 supporting components for the
  basket composer / continuous + option leg pickers)

### Services
- `TcgUserSettingsService` — theme / chart-type / RFR (feature-scoped via
  the parent route in `tcgRoutes`)
- `TcgAutosaveService` — debounced + flush-on-unload save (component-scoped)
- `TcgBackendAutosaveService` — debounced backend autosave with abort +
  in-flight coalescing (component-scoped)
- `TcgAbortableActionService` — `AbortController` lifecycle helper
  (component-scoped)
- `TcgPlotlyService` — Plotly lazy-load (component-scoped; bundled with
  `TcgChartComponent.providers`)
- `TcgDataApi`, `TcgPersistenceApi` — feature-scoped HTTP clients

## Constraints (G1–G8)

The library obeys the host-coexistence rules documented in the parent
`CLAUDE.md`:
- No `BrowserModule`, no `provideRouter()` / `RouterModule.forRoot()`
- All Angular runtime packages in `peerDependencies`
- Every selector / service / token prefixed `tcg-` / `Tcg` / `TCG_`
- Only `TcgApiService` is `providedIn: 'root'`; everything else is
  component- or feature-scoped
- All API calls go through `TCG_API_BASE_URL`; no hardcoded URLs
- Every component is standalone; no `@NgModule` in the library

## Development (this workspace, not a host)

```bash
cd frontend-angular
npm install
npx ng build tcg-frontend       # ng-packagr build smoke
npx ng build dev-harness        # local app build
npx ng serve dev-harness        # http://localhost:4200
```

The dev-harness expects a backend on `http://localhost:8000`. Use the stub
backend in `tools/dev-stub-backend.py` when MongoDB is unreachable.
