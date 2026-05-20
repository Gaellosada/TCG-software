import { defineConfig, devices } from '@playwright/test';
import * as path from 'path';

/**
 * Playwright configuration for the Angular dev-harness e2e suite.
 *
 * Two `webServer` entries:
 *   1. The stub backend (python http.server) serving canned JSON for
 *      every endpoint the Data + Settings pages exercise.
 *   2. The dev-harness `ng serve` instance proxied at port 4200.
 *
 * Both are started by Playwright when `npx playwright test` runs (and
 * gracefully reused if already up). All tests inherit `baseURL` so they
 * can use relative paths like `await page.goto('/data')`.
 *
 * Browser: chromium only (no firefox/webkit configured — WSL2 doesn't
 * ship them by default). Tests are written defensively so they degrade
 * to a clear failure if the browser stack is unavailable.
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60000,
  expect: { timeout: 10000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:4200',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    actionTimeout: 10000,
    navigationTimeout: 30000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: [
    {
      command: 'python3 tools/dev-stub-backend.py --port 8000',
      port: 8000,
      cwd: path.resolve(__dirname),
      reuseExistingServer: !process.env['CI'],
      timeout: 30000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: 'npx ng serve dev-harness --port 4200 --host 127.0.0.1 --hmr=false',
      port: 4200,
      cwd: path.resolve(__dirname),
      reuseExistingServer: !process.env['CI'],
      timeout: 180000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],
});
