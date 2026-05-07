import { test, expect } from '@playwright/test';

// End-to-end spec for the MongoDB Agent page.
//
// Mocks every backend endpoint so no real server is needed (beyond the Vite
// dev server for serving the SPA). Follows the same pattern as signals.spec.js.
const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';

const MOCK_SESSIONS = [
  { id: 'test-session-1', name: 'SPX SMA Backtest', created_at: '2026-05-04T12:00:00Z' },
  { id: 'test-session-2', name: 'VIX Put Strategy', created_at: '2026-05-03T10:00:00Z' },
];

test.describe('Agent page', () => {
  test.beforeEach(async ({ page }) => {
    // Mock session list + create
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
          body: JSON.stringify({
            id: 'new-session-id',
            name: 'New Session',
            created_at: new Date().toISOString(),
          }),
        });
      } else {
        await route.continue();
      }
    });

    // Mock delete endpoint
    await page.route('**/api/agent/sessions/*', async (route, request) => {
      if (request.method() === 'DELETE') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ status: 'deleted' }),
        });
      } else {
        await route.continue();
      }
    });

    // Mock agent health
    await page.route('**/api/agent/health', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ available: true, model: 'claude-sonnet-4-20250514' }),
      });
    });

    // Mock notebook endpoint
    await page.route('**/api/agent/sessions/*/notebook', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ cells: [] }),
      });
    });

    // Mock assumptions endpoint
    await page.route('**/api/agent/sessions/*/assumptions', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([]),
      });
    });

    // Mock data endpoints the app may call on load (collections sidebar)
    await page.route('**/api/data/collections*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ collections: [] }),
      });
    });
  });

  test('page loads with sidebar agent section and 3-panel layout', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

    await page.goto(`${BASE}/agent`);

    // Sidebar "Agents" section label visible
    await expect(page.getByText('Agents')).toBeVisible();

    // "MongoDB Agent" link exists and is active (has active class or aria-current)
    const agentLink = page.getByRole('link', { name: 'MongoDB Agent' });
    await expect(agentLink).toBeVisible();
    await expect(agentLink).toHaveAttribute('aria-current', 'page');

    // 3-panel layout: session panel (left top), assumptions panel (left bottom),
    // chat/notebook panel (right). Verify key elements from each.
    await expect(page.getByText('Sessions')).toBeVisible();
    await expect(page.getByText('Assumptions', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /chat/i })).toBeVisible();

    expect(consoleErrors).toEqual([]);
  });

  test('session list displays mock sessions and selection works', async ({ page }) => {
    await page.goto(`${BASE}/agent`);

    // Both mock sessions appear
    await expect(page.getByText('SPX SMA Backtest')).toBeVisible();
    await expect(page.getByText('VIX Put Strategy')).toBeVisible();

    // Click the first session — it should get selected styling
    const firstSession = page.getByText('SPX SMA Backtest');
    await firstSession.click();

    // After selection the chat panel should show the empty conversation state
    await expect(page.getByText('Start a conversation...')).toBeVisible();
  });

  test('create new session', async ({ page }) => {
    await page.goto(`${BASE}/agent`);

    // Wait for sessions to load
    await expect(page.getByText('SPX SMA Backtest')).toBeVisible();

    // Click the "+ New" button
    const newBtn = page.getByRole('button', { name: /new session/i });
    await expect(newBtn).toBeVisible();
    await newBtn.click();

    // After create, the session list re-fetches — mock returns same list
    // The test verifies the button was clickable and no error appears
    await expect(page.getByText('SPX SMA Backtest')).toBeVisible();
  });

  test('tab switching between Chat and Notebook', async ({ page }) => {
    await page.goto(`${BASE}/agent`);

    // Chat tab is active by default — the chat panel shows empty state
    const chatTab = page.getByRole('button', { name: /chat/i });
    const notebookTab = page.getByRole('button', { name: /notebook/i });

    await expect(chatTab).toBeVisible();
    await expect(notebookTab).toBeVisible();

    // Chat content visible (empty state or textarea)
    await expect(page.getByText('Start a conversation...')).toBeVisible();

    // S1 fix (Issue 22): notebook tab is now disabled until a session with a
    // notebook is selected. Select a session first so the probe resolves (mock
    // returns 200 for /notebook → notebookReady=true → tab enabled).
    await page.getByText('SPX SMA Backtest').click();
    await expect(notebookTab).toBeEnabled({ timeout: 10000 });

    // Switch to Notebook tab
    await notebookTab.click();

    // Notebook panel is visible (empty notebook — cells:[]); header shows Refresh button
    await expect(page.getByRole('button', { name: /refresh/i })).toBeVisible({ timeout: 10000 });

    // Switch back to Chat
    await chatTab.click();
    await expect(page.getByText('Start a conversation...')).toBeVisible();
  });

  test('chat panel structure — textarea and connection indicator', async ({ page }) => {
    await page.goto(`${BASE}/agent`);

    // Select a session first to get the full chat panel
    await page.getByText('SPX SMA Backtest').click();

    // Textarea is visible
    const textarea = page.locator('textarea');
    await expect(textarea).toBeVisible();

    // Connection indicator dot is present (title attribute)
    const connectionDot = page.locator('[title="Connected"], [title="Disconnected"]');
    await expect(connectionDot).toBeVisible();

    // Send button is visible
    const sendBtn = page.getByRole('button', { name: /send message/i });
    await expect(sendBtn).toBeVisible();

    // Empty state shown when no messages
    await expect(page.getByText('Start a conversation...')).toBeVisible();
  });

  test('assumptions panel shows empty state', async ({ page }) => {
    await page.goto(`${BASE}/agent`);

    // The assumptions panel shows "No assumptions yet" text
    await expect(page.getByText(/no assumptions yet/i)).toBeVisible();
  });

  test('notebook panel shows select-session state when no session', async ({ page }) => {
    await page.goto(`${BASE}/agent`);

    // S1 fix (Issue 22): notebook tab is disabled when no session is selected.
    // The tab carries a tooltip "No notebook available" to convey the disabled state.
    // We verify the disabled affordance rather than clicking a disabled button.
    const notebookTab = page.getByRole('button', { name: /notebook/i });
    await expect(notebookTab).toBeDisabled();
    await expect(notebookTab).toHaveAttribute('aria-disabled', 'true');
  });

  test('sidebar navigation structure', async ({ page }) => {
    await page.goto(`${BASE}/agent`);

    // Top section: Data, Indicators, Signals, Portfolio
    await expect(page.getByRole('link', { name: 'Data' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Indicators' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Signals' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Portfolio' })).toBeVisible();

    // Middle section: MongoDB Agent
    await expect(page.getByRole('link', { name: 'MongoDB Agent' })).toBeVisible();

    // Bottom section: Help, Settings
    await expect(page.getByRole('link', { name: 'Help' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Settings' })).toBeVisible();

    // Navigate to Data
    await page.getByRole('link', { name: 'Data' }).click();
    await expect(page).toHaveURL(/\/data/);

    // Navigate back to Agent
    await page.getByRole('link', { name: 'MongoDB Agent' }).click();
    await expect(page).toHaveURL(/\/agent/);
  });
});
