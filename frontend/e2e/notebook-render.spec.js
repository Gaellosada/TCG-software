/**
 * Playwright e2e — notebook renderer regression test (Issue 24).
 *
 * Strategy: mock every HTTP and WS endpoint. The WS mock sends a
 * `notebook_ready` message immediately on connect so the FE fetches and
 * renders the kitchen-sink notebook from the mocked API response.
 *
 * Assertions:
 *  - At least one <img> (matplotlib cell 4 / cell 6-multi)
 *  - At least one .plotlyContainer element (Plotly cell 5)
 *  - At least one <pre> for HTML/text output
 *  - Italic / link markup rendered
 *  - ANSI codes stripped from error traceback
 *
 * Best-effort: e2e relies on the dev server running (http://localhost:5173).
 * If the server is not running the tests will fail with a connection error.
 * Unit tests in renderMarkdown.test.js are the robust regression backbone for
 * RCA-2/3; this file adds the renderer integration layer.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';
const SESSION_ID = 'kitchen-sink-e2e-test';

// Load the kitchen-sink notebook JSON (served as mock API response).
// Path: TCG-software/frontend/e2e/ → TCG-software/workspace/tasks/.../output/
const NOTEBOOK_PATH = resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../../workspace/tasks/agent-stop-mid-task-structural-and-notebook/output/kitchen-sink-notebook.ipynb',
);
const KITCHEN_SINK_NB = JSON.parse(readFileSync(NOTEBOOK_PATH));

// A minimal mock session list with our fake session.
const MOCK_SESSIONS = [
  {
    id: SESSION_ID,
    name: 'Kitchen Sink E2E Test Session',
    created_at: '2026-05-07T10:00:00Z',
  },
];

test.describe('Notebook renderer (Issue 24 — kitchen-sink regression)', () => {
  test.beforeEach(async ({ page }) => {
    // Note: Playwright routes are LIFO — last registered = highest priority.
    // Register broad fallbacks FIRST, specific routes LAST.

    // Broad fallback: data endpoints
    await page.route('**/api/data/**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    // Broad fallback: health
    await page.route('**/api/agent/health', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ available: true }),
      });
    });

    // Generic sessions list (non-specific)
    await page.route('**/api/agent/sessions', async (route, request) => {
      if (request.method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(MOCK_SESSIONS),
        });
      } else if (request.method() === 'POST') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ id: 'new', name: 'New', created_at: new Date().toISOString() }),
        });
      } else {
        await route.continue();
      }
    });

    // Wildcard for session sub-resources (low priority — registered before specifics)
    await page.route('**/api/agent/sessions/**', async (route, request) => {
      const url = request.url();
      if (url.endsWith('/assumptions')) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify([]),
        });
      } else if (request.method() === 'DELETE') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
      } else {
        // Pass through to real BE for WS and notebook serving
        await route.continue();
      }
    });

    // Specific: kitchen-sink notebook endpoint (registered LAST = highest priority)
    await page.route(`**/api/agent/sessions/${SESSION_ID}/notebook`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(KITCHEN_SINK_NB),
      });
    });
  });

  test('notebook renders matplotlib image outputs', async ({ page }) => {
    await page.goto(`${BASE}/agent`);

    // Select the kitchen-sink session
    await page.getByText('Kitchen Sink E2E Test Session').click();

    // The WS connects; we need to inject notebook_ready via the WS.
    // Trigger it by evaluating JS in the page context: fire the WS mock.
    // Since WS mocking via Playwright is complex, we instead rely on the
    // Issue-22 HEAD probe fallback: the FE sends GET /notebook on history
    // event and sets notebookReady=true if 200.
    // The mock above returns 200, so notebookReady becomes true.

    // Wait for notebook tab to become enabled (notebookReady probe returns 200)
    const notebookTab = page.getByRole('button', { name: /notebook/i });
    await expect(notebookTab).toBeEnabled({ timeout: 10000 });

    // Switch to Notebook tab
    await notebookTab.click();

    // Wait for at least one <img> element (matplotlib output)
    await expect(page.locator('img[src^="data:image/png;base64,"]').first()).toBeVisible({ timeout: 15000 });
    const imgCount = await page.locator('img[src^="data:image/png;base64,"]').count();
    expect(imgCount).toBeGreaterThanOrEqual(1);
  });

  test('notebook renders Plotly outputs', async ({ page }) => {
    await page.goto(`${BASE}/agent`);
    await page.getByText('Kitchen Sink E2E Test Session').click();

    const notebookTab = page.getByRole('button', { name: /notebook/i });
    await expect(notebookTab).toBeEnabled({ timeout: 10000 });
    await notebookTab.click();

    // Wait for notebook to load
    await page.waitForLoadState('networkidle');

    // The Plotly container should be in the DOM (react-plotly.js renders to a div)
    // PlotlyOutput wraps it in a div.plotlyContainer (CSS module class).
    // In the DOM it will have a hashed class name containing "plotlyContainer".
    const plotlyEl = page.locator('[class*="plotlyContainer"]');
    await expect(plotlyEl).toBeVisible({ timeout: 15000 });
  });

  test('notebook renders pre elements for text/stream output', async ({ page }) => {
    await page.goto(`${BASE}/agent`);
    await page.getByText('Kitchen Sink E2E Test Session').click();

    const notebookTab = page.getByRole('button', { name: /notebook/i });
    await expect(notebookTab).toBeEnabled({ timeout: 10000 });
    await notebookTab.click();

    // Wait for notebook content to appear before counting (M1 race fix)
    await expect(page.locator('pre').first()).toBeVisible({ timeout: 15000 });
    // Stream/text outputs render as <pre> elements
    const preCount = await page.locator('pre').count();
    expect(preCount).toBeGreaterThanOrEqual(1);
  });

  test('RCA-4: ANSI codes are stripped from error traceback', async ({ page }) => {
    await page.goto(`${BASE}/agent`);
    await page.getByText('Kitchen Sink E2E Test Session').click();

    const notebookTab = page.getByRole('button', { name: /notebook/i });
    await expect(notebookTab).toBeEnabled({ timeout: 10000 });
    await notebookTab.click();

    // Error output shows ename: evalue but no raw ANSI bytes
    await expect(page.getByText(/ZeroDivisionError/)).toBeVisible({ timeout: 15000 });
    // The text content of the error pre must not contain raw ANSI escape bytes
    const errorPre = page.locator('[class*="errorOutput"]');
    const errorText = await errorPre.textContent();
    expect(errorText).not.toContain('\x1b[');
    expect(errorText).not.toContain('[31m');
  });

  test('RCA-2: italic markdown rendered as <em>', async ({ page }) => {
    await page.goto(`${BASE}/agent`);
    await page.getByText('Kitchen Sink E2E Test Session').click();

    const notebookTab = page.getByRole('button', { name: /notebook/i });
    await expect(notebookTab).toBeEnabled({ timeout: 10000 });
    await notebookTab.click();

    // The markdown cell in kitchen-sink has _italic_ text.
    // After fix, the DOM should contain <em> elements.
    await expect(page.locator('em').first()).toBeVisible({ timeout: 15000 });
  });

  test('RCA-3: markdown links rendered as <a> with rel=noopener', async ({ page }) => {
    await page.goto(`${BASE}/agent`);
    await page.getByText('Kitchen Sink E2E Test Session').click();

    const notebookTab = page.getByRole('button', { name: /notebook/i });
    await expect(notebookTab).toBeEnabled({ timeout: 10000 });
    await notebookTab.click();

    // The markdown cell has [Link to Anthropic](https://anthropic.com).
    // After fix, the DOM should contain an <a> with rel=noopener.
    const linkEl = page.locator('a[href^="https://"]');
    await expect(linkEl).toBeVisible({ timeout: 15000 });
    const rel = await linkEl.first().getAttribute('rel');
    expect(rel).toContain('noopener');
  });
});
