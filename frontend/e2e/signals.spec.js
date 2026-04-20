import { test, expect } from '@playwright/test';

// End-to-end spec for the Signals page (iter-3 v2 contract).
// Mocks every backend endpoint so no real server is needed.
const BASE = 'http://localhost:5173';

test.describe('Signals page (v2)', () => {
  test.beforeEach(async ({ page }) => {
    // Seed the Indicators localStorage with one user indicator.
    await page.addInitScript(() => {
      try {
        if (window.localStorage.getItem('tcg.indicators.v1')) return;
        window.localStorage.setItem('tcg.indicators.v1', JSON.stringify({
          version: 1,
          indicators: [
            {
              id: 'my-sma',
              name: 'My SMA',
              code: "def compute(series, window: int = 5):\n    return series['price']",
              doc: '',
              params: { window: 5 },
              seriesMap: { price: { collection: 'INDEX', instrument_id: '^GSPC' } },
              ownPanel: false,
            },
          ],
          defaultState: {},
        }));
      } catch { /* ignore */ }
    });

    await page.route('**/api/data/collections*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ collections: ['INDEX'] }),
      });
    });
    await page.route('**/api/data/INDEX*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' }],
          total: 1,
          skip: 0,
          limit: 500,
        }),
      });
    });

    // v2 mock — positions array, clipped flag, timestamps in unix ms.
    await page.route('**/api/signals/compute', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          timestamps: [1577923200000, 1578009600000, 1578268800000],
          positions: [
            {
              instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
              values: [0, 1, 0],
              clipped_mask: [false, false, false],
              price: { label: '^GSPC.close', values: [3200, 3250, null] },
            },
          ],
          clipped: false,
          diagnostics: {},
        }),
      });
    });
  });

  test('renders /signals with three panels and no console errors', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

    await page.goto(`${BASE}/signals`);
    await expect(page.getByRole('main').getByText('Signals', { exact: true })).toBeVisible();
    await expect(page.getByTestId('add-signal-btn')).toBeVisible();
    await expect(page.getByText(/Select a signal on the left/)).toBeVisible();

    expect(consoleErrors).toEqual([]);
  });

  test('create signal, add block with no defaults (empty instrument, 0 weight)', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();
    await expect(page.getByTestId('block-editor')).toBeVisible();

    // All four direction tabs.
    await expect(page.getByTestId('direction-tab-long_entry')).toBeVisible();
    await expect(page.getByTestId('direction-tab-long_exit')).toBeVisible();
    await expect(page.getByTestId('direction-tab-short_entry')).toBeVisible();
    await expect(page.getByTestId('direction-tab-short_exit')).toBeVisible();

    // Add a block — iter-3 no defaults: empty instrument + no conditions.
    await page.getByTestId('add-block-btn').click();
    await expect(page.getByTestId('block-0')).toBeVisible();
    // Empty instrument button present.
    await expect(page.getByTestId('block-instrument-btn-0')).toBeVisible();
    // Weight input visible on entry tabs.
    await expect(page.getByTestId('block-weight-0')).toBeVisible();
    // Run is disabled (no instrument, no conditions).
    await expect(page.getByTestId('run-signal-btn')).toBeDisabled();

    expect(consoleErrors).toEqual([]);
  });

  test('persists signals across reload', async ({ page }) => {
    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();
    await page.waitForTimeout(800);
    await page.reload();
    await expect(page.locator('[data-testid^="signal-row-"]').first()).toBeVisible();
  });

  test('block delete uses a confirmation dialog (no window.confirm)', async ({ page }) => {
    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();
    await page.getByTestId('add-block-btn').click();
    await expect(page.getByTestId('block-0')).toBeVisible();

    // Hook window.confirm — the test asserts it is NEVER invoked.
    await page.evaluate(() => {
      window.__confirmCalls = 0;
      const orig = window.confirm;
      window.confirm = (...args) => { window.__confirmCalls += 1; return orig.apply(window, args); };
    });

    await page.getByTestId('remove-block-0').click();
    // Modal confirm dialog appears.
    await expect(page.getByTestId('confirm-dialog')).toBeVisible();
    await page.getByTestId('confirm-dialog-cancel').click();
    await expect(page.getByTestId('confirm-dialog')).not.toBeVisible();

    const confirmCalls = await page.evaluate(() => window.__confirmCalls);
    expect(confirmCalls).toBe(0);
  });
});
