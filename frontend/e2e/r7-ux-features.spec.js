/**
 * Playwright e2e — Round-7 UX features
 *
 * Tests:
 *   R7-T1: notebook_failed state — tab shows warning indicator (amber dot)
 *          and data-state="failed"; panel shows failure explanation.
 *   R7-T2: notebook_ready after notebook_failed clears failed state.
 *   R7-T3: tab title prefixed with "● " when page hidden and target event fires.
 *   R7-T4: tab title restored on visibilitychange to visible.
 *   R7-T5: Notifications permission requested only after first turn_complete.
 *   R7-T6: Bundle content — TURN_HANDOFF_DONE and notebook_failed present in dist.
 *
 * Strategy:
 *   WS events are injected via page.evaluate() by finding the WS instance
 *   stored on window (we expose it from a test-only shim, OR we use the
 *   WebSocket mock approach: intercept WS creation and return a controller).
 *
 *   Simpler approach: use Playwright's ability to evaluate JS to dispatch
 *   custom events that the React hook picks up via page-level WS message
 *   simulation using a test-only WS intercept.
 *
 * R7-T6 is a static file check that does NOT require the dev server.
 */

import { test, expect } from '@playwright/test';
import { readFileSync, existsSync, readdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';
const SESSION_ID = 'r7-ux-test-session';

const MOCK_SESSIONS = [
  {
    id: SESSION_ID,
    name: 'R7 UX Test Session',
    created_at: '2026-05-07T10:00:00Z',
  },
];

/**
 * Setup common mocks for the agent page.
 * Returns a WebSocket mock controller that can inject messages.
 */
async function setupAgentPageMocks(page, notebookStatus = '200') {
  await page.route('**/api/data/**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
  );
  await page.route('**/api/agent/health', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '{"available":true}' }),
  );
  await page.route('**/api/agent/sessions', async (route, request) => {
    if (request.method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_SESSIONS),
      });
    } else {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'new', name: 'New', created_at: new Date().toISOString() }),
      });
    }
  });
  await page.route('**/api/agent/sessions/*/assumptions', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );

  // Notebook endpoint: 200 or 422 depending on the test scenario.
  await page.route(`**/api/agent/sessions/${SESSION_ID}/notebook`, async (route) => {
    if (notebookStatus === '422') {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'notebook_no_outputs', message: 'no outputs' }),
      });
    } else {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ cells: [] }),
      });
    }
  });
}

/** Inject a WS message into the open WS connection via page.evaluate. */
async function injectWsMessage(page, payload) {
  await page.evaluate((msg) => {
    // The app uses native WebSocket; intercept the first open WS and dispatch
    // a message event from its perspective. We use a global we install below.
    if (window.__tcgTestWs) {
      window.__tcgTestWs.dispatchEvent(
        new MessageEvent('message', { data: JSON.stringify(msg) }),
      );
    }
  }, payload);
}

/** Install a WS interceptor that captures the connection object for test injection. */
async function installWsInterceptor(page) {
  await page.addInitScript(() => {
    const OrigWS = window.WebSocket;
    window.WebSocket = class extends OrigWS {
      constructor(...args) {
        super(...args);
        // Only capture agent WS connections.
        if (args[0] && String(args[0]).includes('/ws/agent/')) {
          window.__tcgTestWs = this;
        }
      }
    };
  });
}

/* ── R7-T1: notebook_failed tab state ──────────────────────── */

test.describe('R7 — notebook_failed tab state', () => {
  test('tab shows data-state=failed and amber dot when notebook_failed fires', async ({ page }) => {
    await installWsInterceptor(page);
    // 422 on notebook GET: HEAD-probe fails → tab stays disabled initially.
    await setupAgentPageMocks(page, '422');

    await page.goto(`${BASE}/agent`);
    await page.getByText('R7 UX Test Session').click();

    // Tab is initially disabled (422 → probe .catch() → notebookReady stays false).
    const notebookTab = page.getByRole('button', { name: /notebook/i });
    await expect(notebookTab).toBeDisabled({ timeout: 8000 });

    // Inject notebook_failed WS event.
    await injectWsMessage(page, {
      type: 'notebook_failed',
      session_id: SESSION_ID,
      reason: 'no_outputs',
      detail: 'Notebook has 7 code cells, all with empty outputs[].',
      timestamp: new Date().toISOString(),
    });

    // After notebook_failed: tab should be enabled (clickable) but in failed state.
    await expect(notebookTab).toBeEnabled({ timeout: 5000 });
    await expect(notebookTab).toHaveAttribute('data-state', 'failed');
    // Tooltip text.
    await expect(notebookTab).toHaveAttribute(
      'title',
      'Notebook compilation failed — no outputs detected',
    );

    // Click opens the panel showing the failure explanation.
    await notebookTab.click();
    await expect(page.getByTestId('notebook-failed-panel')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText(/notebook compilation failed/i)).toBeVisible();
  });

  test('notebook_ready after notebook_failed restores normal state', async ({ page }) => {
    await installWsInterceptor(page);
    await setupAgentPageMocks(page, '422');

    await page.goto(`${BASE}/agent`);
    await page.getByText('R7 UX Test Session').click();

    const notebookTab = page.getByRole('button', { name: /notebook/i });

    // Inject notebook_failed.
    await injectWsMessage(page, {
      type: 'notebook_failed',
      session_id: SESSION_ID,
      reason: 'no_outputs',
      timestamp: new Date().toISOString(),
    });

    await expect(notebookTab).toHaveAttribute('data-state', 'failed', { timeout: 5000 });

    // Inject notebook_ready — clears failed state.
    await injectWsMessage(page, {
      type: 'notebook_ready',
      session_id: SESSION_ID,
    });

    // data-state should no longer be "failed".
    await expect(notebookTab).not.toHaveAttribute('data-state', 'failed', { timeout: 5000 });
    // Tab should be enabled.
    await expect(notebookTab).toBeEnabled();
  });
});

/* ── R7-T3/T4: Tab title alert ────────────────────────────── */

test.describe('R7 — tab title prefix on target events', () => {
  test('document.title gets "● " prefix when page hidden and turn_complete fires', async ({ page }) => {
    await installWsInterceptor(page);
    await setupAgentPageMocks(page, '200');

    await page.goto(`${BASE}/agent`);
    await page.getByText('R7 UX Test Session').click();

    // Simulate page hidden.
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    });

    const originalTitle = await page.title();

    // Inject turn_complete.
    await injectWsMessage(page, {
      type: 'turn_complete',
      session_id: SESSION_ID,
      elapsed_seconds: 5,
      timestamp: new Date().toISOString(),
    });

    // Wait a tick for the React effect to fire.
    await page.waitForTimeout(200);

    const newTitle = await page.title();
    expect(newTitle).toContain('●');
    expect(newTitle).toContain(originalTitle.replace(/^● /, ''));
  });

  test('document.title restored on visibilitychange to visible', async ({ page }) => {
    await installWsInterceptor(page);
    await setupAgentPageMocks(page, '200');

    await page.goto(`${BASE}/agent`);
    await page.getByText('R7 UX Test Session').click();

    const originalTitle = await page.title();

    // Simulate page hidden and inject event.
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    });
    await injectWsMessage(page, {
      type: 'turn_complete',
      session_id: SESSION_ID,
      elapsed_seconds: 3,
      timestamp: new Date().toISOString(),
    });
    await page.waitForTimeout(200);

    // Now simulate becoming visible.
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await page.waitForTimeout(200);

    const restoredTitle = await page.title();
    // Title should no longer have the prefix.
    expect(restoredTitle).not.toMatch(/^●/);
  });
});

/* ── R7-T5: Notifications permission ────────────────────────── */

test.describe('R7 — Notifications API permission', () => {
  test('permission NOT requested before turn_complete', async ({ page }) => {
    await installWsInterceptor(page);
    await setupAgentPageMocks(page, '200');

    // Track Notification.requestPermission calls.
    await page.addInitScript(() => {
      window.__notifPermCalls = 0;
      const MockNotification = function () {};
      MockNotification.permission = 'default';
      MockNotification.requestPermission = () => {
        window.__notifPermCalls++;
        return Promise.resolve('default');
      };
      window.Notification = MockNotification;
    });

    await page.goto(`${BASE}/agent`);
    await page.getByText('R7 UX Test Session').click();

    // No permission request yet.
    const callsBefore = await page.evaluate(() => window.__notifPermCalls);
    expect(callsBefore).toBe(0);
  });

  test('permission requested on first turn_complete', async ({ page }) => {
    await installWsInterceptor(page);
    await setupAgentPageMocks(page, '200');

    await page.addInitScript(() => {
      window.__notifPermCalls = 0;
      const MockNotification = function () {};
      MockNotification.permission = 'default';
      MockNotification.requestPermission = () => {
        window.__notifPermCalls++;
        return Promise.resolve('default');
      };
      window.Notification = MockNotification;
      localStorage.removeItem('tcg_notif_perm_asked');
    });

    await page.goto(`${BASE}/agent`);
    await page.getByText('R7 UX Test Session').click();

    await injectWsMessage(page, {
      type: 'turn_complete',
      session_id: SESSION_ID,
      elapsed_seconds: 2,
      timestamp: new Date().toISOString(),
    });

    await page.waitForTimeout(300);
    const callsAfter = await page.evaluate(() => window.__notifPermCalls);
    expect(callsAfter).toBe(1);
  });

  test('permission NOT re-requested when localStorage flag is set', async ({ page }) => {
    await installWsInterceptor(page);
    await setupAgentPageMocks(page, '200');

    await page.addInitScript(() => {
      window.__notifPermCalls = 0;
      const MockNotification = function () {};
      MockNotification.permission = 'default';
      MockNotification.requestPermission = () => {
        window.__notifPermCalls++;
        return Promise.resolve('default');
      };
      window.Notification = MockNotification;
      // Pre-set the flag.
      localStorage.setItem('tcg_notif_perm_asked', '1');
    });

    await page.goto(`${BASE}/agent`);
    await page.getByText('R7 UX Test Session').click();

    await injectWsMessage(page, {
      type: 'turn_complete',
      session_id: SESSION_ID,
      elapsed_seconds: 2,
      timestamp: new Date().toISOString(),
    });

    await page.waitForTimeout(300);
    const callsAfter = await page.evaluate(() => window.__notifPermCalls);
    expect(callsAfter).toBe(0);
  });
});

/* ── R7-T6: Bundle content verification ─────────────────────── */

test.describe('R7 — Bundle content verification', () => {
  test('dist bundle contains R6 fix (TURN_HANDOFF_DONE string)', () => {
    const distDir = resolve(__dirname, '../dist/assets');
    const files = readdirSync(distDir).filter((f) => f.endsWith('.js') && f.startsWith('index'));
    expect(files.length).toBeGreaterThan(0);

    const bundlePath = resolve(distDir, files[0]);
    const content = readFileSync(bundlePath, 'utf8');

    // TURN_HANDOFF_DONE is a string literal in stripDoneMarker — survives minification.
    expect(content).toContain('TURN_HANDOFF_DONE');
  });

  test('dist bundle contains R7 notebook_failed string', () => {
    const distDir = resolve(__dirname, '../dist/assets');
    const files = readdirSync(distDir).filter((f) => f.endsWith('.js') && f.startsWith('index'));
    expect(files.length).toBeGreaterThan(0);

    const bundlePath = resolve(distDir, files[0]);
    const content = readFileSync(bundlePath, 'utf8');

    // "notebook_failed" is used as a case label in the WS switch — survives minification.
    expect(content).toContain('notebook_failed');
  });
});
