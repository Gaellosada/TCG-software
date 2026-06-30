import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 60000,
  use: {
    baseURL: 'http://localhost:5173',
    headless: true,
    screenshot: 'only-on-failure',
  },
  // Start THIS worktree's Vite so the e2e is deterministic. Without this the
  // suite silently ran against whatever already served :5173 — which could be
  // a different checkout (a latent false pass/fail; see PR #69 review B MINOR).
  // ``--strictPort`` makes Vite fail loudly instead of drifting to another
  // port (which would re-introduce the wrong-checkout fragility). Locally we
  // reuse an existing :5173 server for fast iteration; in CI we always boot a
  // fresh one so the run can never depend on ambient state.
  webServer: {
    command: 'npm run dev -- --port 5173 --strictPort',
    url: 'http://localhost:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
