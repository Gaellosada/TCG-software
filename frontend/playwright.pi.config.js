import { defineConfig } from '@playwright/test';

// Dedicated config for the per-instrument-data-source option-add→compute
// end-to-end suite (option-source-add-compute.spec.js).
//
// Unlike the default playwright.config.js (which boots its OWN Vite on :5173),
// this config assumes an ALREADY-RUNNING frontend + backend that you manage
// yourself:
//   - backend on :8000 (source-aware catalog, live warehouse)
//   - a Vite dev server proxying /api → :8000, e.g.
//       npm run dev -- --port 5200 --strictPort
// Point the suite at it via TCG_E2E_BASE (default http://localhost:5200).
//
// No webServer here on purpose: the suite must NOT boot or touch the shared
// :5173 / :8000 app. Screenshots land in e2e/__screens__/.
export default defineConfig({
  testDir: './e2e',
  testMatch: 'option-source-add-compute.spec.js',
  timeout: 240000,
  expect: { timeout: 20000 },
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: process.env.TCG_E2E_BASE || 'http://localhost:5200',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
});
