# Angular dev-harness e2e suite

Playwright tests against the `dev-harness` Angular app + the canned
`dev-stub-backend.py`. These specs are the Wave V e2e port for the
seed pages (Data + Settings).

## Running

```bash
cd frontend-angular
npx playwright install chromium      # one-time browser install
npx playwright test                  # runs the suite (auto-starts both servers)
```

Playwright's `webServer` config starts the python stub backend on port
8000 and `ng serve dev-harness` on port 4200; both are torn down at
process exit.

## WSL2 caveat

In environments without outbound network access (such as the WSL2 host
used by this repo's CI), `npx playwright install` cannot reach the
Playwright CDN. The cached chromium build at
`~/.cache/ms-playwright/chromium-1217/` matches Playwright 1.59.x and
works without further downloads. Higher Playwright versions require a
newer chromium build than what is cached.

## Specs

- `quick-check.spec.ts` — sanity probe: dev-harness loads, sidebar
  renders, no console errors on initial load.
- `data-settings.spec.ts` — minimal cross-page flow: switch from Data
  to Settings, toggle theme, return to Data, theme persists.
