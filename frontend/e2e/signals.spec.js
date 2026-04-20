import { test, expect } from '@playwright/test';

// End-to-end spec for the Signals page (iter-4 v3 contract).
//
// v3: signals declare first-class named inputs. Blocks reference them
// by input_id. Instrument/indicator operands reference inputs by id too.
// Mocks every backend endpoint so no real server is needed.
const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';

test.describe('Signals page (v3)', () => {
  test.beforeEach(async ({ page }) => {
    // Seed the Indicators localStorage with one user indicator and
    // force a clean v3 signals slate (discard any stale v2).
    await page.addInitScript(() => {
      try {
        window.localStorage.removeItem('tcg.signals.v2');
        if (!window.localStorage.getItem('tcg.indicators.v1')) {
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
        }
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

    // v3 mock — positions have input_id + typed instrument; indicators is
    // a LIST (iter-3 contract), clipped is a bool.
    await page.route('**/api/signals/compute', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          timestamps: [1577923200000, 1578009600000, 1578268800000],
          positions: [
            {
              input_id: 'X',
              instrument: { type: 'spot', collection: 'INDEX', instrument_id: '^GSPC' },
              values: [0, 1, 0],
              clipped_mask: [false, false, false],
              price: { label: '^GSPC.close', values: [3200, 3250, null] },
            },
          ],
          indicators: [],
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

  test('create signal shows InputsPanel expanded by default (no inputs)', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();
    // v3: InputsPanel sits above the BlockEditor.
    await expect(page.getByTestId('inputs-panel')).toBeVisible();
    await expect(page.getByTestId('inputs-add-btn')).toBeVisible();
    await expect(page.getByTestId('block-editor')).toBeVisible();

    expect(consoleErrors).toEqual([]);
  });

  test('add input then add block — block header exposes input-id select', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();

    // Add an input (starts as an unconfigured spot).
    await page.getByTestId('inputs-add-btn').click();
    await expect(page.getByTestId('input-row-0')).toBeVisible();

    // Add a block — v3 no defaults: empty input_id, weight 0, no conditions.
    await page.getByTestId('add-block-btn').click();
    await expect(page.getByTestId('block-0')).toBeVisible();

    // v3: block header has an input-id <select>, not an instrument popover.
    const select = page.getByTestId('block-input-select-0');
    await expect(select).toBeVisible();
    // Weight input visible on entry tabs.
    await expect(page.getByTestId('block-weight-0')).toBeVisible();

    // All four direction tabs still present.
    await expect(page.getByTestId('direction-tab-long_entry')).toBeVisible();
    await expect(page.getByTestId('direction-tab-long_exit')).toBeVisible();
    await expect(page.getByTestId('direction-tab-short_entry')).toBeVisible();
    await expect(page.getByTestId('direction-tab-short_exit')).toBeVisible();

    // Run is disabled (input not configured, block not runnable).
    await expect(page.getByTestId('run-signal-btn')).toBeDisabled();

    expect(consoleErrors).toEqual([]);
  });

  test('persists signals across reload (v3 schema)', async ({ page }) => {
    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();
    // Let autosave flush.
    await page.waitForTimeout(800);
    await page.reload();
    await expect(page.locator('[data-testid^="signal-row-"]').first()).toBeVisible();

    // localStorage key must be v3.
    const hasV3 = await page.evaluate(() => !!window.localStorage.getItem('tcg.signals.v3'));
    const hasV2 = await page.evaluate(() => !!window.localStorage.getItem('tcg.signals.v2'));
    expect(hasV3).toBe(true);
    expect(hasV2).toBe(false);
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

  test('input delete uses a confirmation dialog too', async ({ page }) => {
    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();
    await page.getByTestId('inputs-add-btn').click();
    await expect(page.getByTestId('input-row-0')).toBeVisible();

    await page.getByTestId('input-delete-0').click();
    await expect(page.getByTestId('confirm-dialog')).toBeVisible();
    await page.getByTestId('confirm-dialog-cancel').click();
    await expect(page.getByTestId('confirm-dialog')).not.toBeVisible();
    // Still there — cancel should not delete.
    await expect(page.getByTestId('input-row-0')).toBeVisible();
  });
});
