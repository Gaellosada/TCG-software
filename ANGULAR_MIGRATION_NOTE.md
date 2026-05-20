# Angular Migration Note — `angular/main` parallel fork

Wave 0 status: **skeleton scaffolded**. Pages ported: **none**.

## Branch model

- Branch `angular/main` lives off `origin/main` @
  `2b1910db92a5353c5f9f23f0a9d49e535fab3863`.
- Never PR'd to `main`. The React frontend on `main` remains canonical.
- Backend (`tcg/`, `tests/`) is READ-ONLY on this branch. `git diff
  origin/main..HEAD -- tcg/ tests/` must remain empty for the life of
  the fork.

## Integration shape

**Source feature module.** A host Angular application pulls this branch
in via git submodule or subtree, then references the library through a
TypeScript path alias:

```json
// host tsconfig.json
{
  "compilerOptions": {
    "paths": {
      "@tcg/frontend": ["./vendor/tcg-software-angular/frontend-angular/projects/tcg-frontend/src/public-api.ts"]
    }
  }
}
```

There is **no** `npm publish` step, no `dist/` artefact intended for
consumption, no `ng-packagr` output beyond the local build smoke. Hosts
import directly from `public-api.ts`.

## Workspace layout

```
TCG-software-angular/                                  worktree root (angular/main)
├── frontend-angular/                                  Angular 19 CLI workspace
│   ├── projects/
│   │   ├── tcg-frontend/                              The library hosts consume
│   │   │   ├── src/
│   │   │   │   ├── lib/
│   │   │   │   │   ├── api/
│   │   │   │   │   │   ├── tcg-api.service.ts         providedIn: 'root'
│   │   │   │   │   │   └── tcg-api.tokens.ts          TCG_API_BASE_URL
│   │   │   │   │   └── tcg-smoke.component.ts         <tcg-smoke> standalone
│   │   │   │   └── public-api.ts                      barrel — sole entry
│   │   │   ├── ng-package.json
│   │   │   ├── package.json                           peerDeps only for @angular/*
│   │   │   └── tsconfig.lib.json
│   │   └── dev-harness/                               local smoke app — NOT shipped
│   │       └── src/
│   │           ├── app/
│   │           │   ├── app.component.{ts,html,css}
│   │           │   ├── app.config.ts                  provides TCG_API_BASE_URL
│   │           │   └── app.routes.ts                  '/' → TcgSmokeComponent
│   │           └── main.ts
│   ├── angular.json
│   ├── package.json
│   └── tsconfig.json                                  paths alias @tcg/frontend
├── frontend-react-archive/                            archived from `frontend/`
├── tcg/                                               FastAPI backend (READ-ONLY)
├── tests/                                             backend tests (READ-ONLY)
├── ANGULAR_MIGRATION_NOTE.md                          this file
└── CLAUDE.md                                          branch override guide
```

## Running the dev-harness

```bash
cd frontend-angular
npm install                          # one-time
npx ng build tcg-frontend            # build library so dev-harness can import it
npx ng serve dev-harness --port=4200 # opens http://localhost:4200
```

The dev-harness expects FastAPI on `http://localhost:8000`. Start it from
the OTHER worktree (the `main` checkout — backend code lives there):

```bash
cd ../TCG-software   # the main worktree
TCG_CORS_ORIGINS="http://localhost:4200,http://localhost:5173" \
  uvicorn tcg.core.app:app --port 8000
```

When FastAPI is reachable, `<tcg-smoke>` will render `Backend status: ok`
with a JSON preview of `/api/data/collections`. When it is unreachable, the
component renders `Backend status: error` with a human-readable diagnostic
— this is the same code path, just with the error branch exercised.

## How a host application consumes the library

1. Add this fork as a git submodule (or `git subtree`) under the host's
   `vendor/` (or any path the host prefers):

   ```bash
   git submodule add -b angular/main \
       https://github.com/<org>/TCG-software.git vendor/tcg-software-angular
   git submodule update --init --recursive
   ```

2. Add a TypeScript path alias in the host's `tsconfig.json`:

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

3. Add `tcg-frontend`'s source root to the host's `angular.json`
   `tsConfig` `include` paths so Angular's compiler walks it.

4. Provide `TCG_API_BASE_URL` and `HttpClient` at host bootstrap:

   ```ts
   import { TCG_API_BASE_URL } from '@tcg/frontend';
   import { provideHttpClient } from '@angular/common/http';

   bootstrapApplication(AppComponent, {
     providers: [
       provideHttpClient(),
       { provide: TCG_API_BASE_URL, useValue: '<your-fastapi-host>' },
       // ... host's own providers
     ],
   });
   ```

5. Import any TCG component as a standalone:

   ```ts
   import { TcgSmokeComponent } from '@tcg/frontend';

   @Component({
     selector: 'host-page',
     standalone: true,
     imports: [TcgSmokeComponent],
     template: `<tcg-smoke></tcg-smoke>`,
   })
   export class HostPage {}
   ```

## Host-coexistence constraints (enforced by guardrails)

| ID | Constraint |
|----|------------|
| G1 | Backend (`tcg/`, `tests/`) is read-only on `angular/main`. Diff vs `origin/main` must remain empty. |
| G2 | Never open or push toward a PR for `angular/main`. Push happens only at Wave D and only the branch — no `gh pr create`. |
| G3 | Library project must NOT import `BrowserModule`, must NOT call `RouterModule.forRoot()` / `provideRouter()` at library level, must NOT register global stylesheets, must declare Angular runtime as `peerDependencies` not `dependencies`. Dev-harness is exempt. |
| G4 | Every public symbol prefixed `tcg-` / `Tcg` / `TCG_`. |
| G5 | `providedIn: 'root'` permitted only on `TcgApiService`. Other services component- or feature-scoped. |
| G6 | API base URL host-configurable via `TCG_API_BASE_URL` injection token. No hardcoded URLs in `TcgApiService` or anywhere else in the library. |

## ASSUMPTIONS made during Wave 0

- **FastAPI `/api/health` endpoint does not exist.** The backend's only
  catch-all-ish GETs are under `/api/data`. The smoke component calls
  `/api/data/collections` instead. If a `/api/health` endpoint is added
  later, swap the URL in `TcgApiService.getHealth()`.

- **MongoDB is not reachable from the WSL dev environment.** The
  `.env` in the main worktree points at `10.0.5.10:27017` which is
  unreachable from this WSL instance, and there is no local MongoDB
  running. As a consequence, when starting real `uvicorn` for the
  smoke, the lifespan blocks on `serverSelectionTimeoutMS=30s` and the
  process exits. Wave 0's smoke uses a stub backend
  (`/tmp/fake_tcg_backend.py`) returning `{"collections": []}` at the
  same path, which exercises the same DI / HttpClient / CORS path. A
  full end-to-end smoke against real FastAPI is deferred to whichever
  later wave runs against a live MongoDB.

- **No headless browser available in the WSL environment.** Wave 0's
  smoke verifies (a) the dev-harness HTML serves, (b) the bundle
  contains `TcgSmokeComponent` and the API URL, and (c) the stub
  backend returns CORS-permitted JSON. Visual / interactive DOM
  verification via Playwright is deferred to Wave V (E2E port).

- **React frontend `frontend/` had no FastAPI static mount.** Reading
  `tcg/core/app.py` confirmed no `app.mount(... StaticFiles ...)` call.
  The archive rename does not break any backend code path.

## Status

| Page | React (`frontend-react-archive/`) | Angular (`frontend-angular/`) |
|------|----------------------------------|------------------------------|
| Data | ✅ ported | ❌ Wave I |
| Settings | ✅ ported | ❌ Wave I |
| Indicators | ✅ ported | ❌ later wave |
| Signals | ✅ ported | ❌ later wave |
| Portfolio | ✅ ported | ❌ later wave |
| Help | ✅ ported | ❌ later wave |
| Tickets | ✅ ported | ❌ later wave |
| RunningSignals | ✅ ported | ❌ later wave |
| MongoDBAgent | ✅ ported | ❌ later wave |

Wave 0 deliverable: skeleton + smoke component only. No pages ported yet.
