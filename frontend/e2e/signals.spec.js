import { test, expect } from '@playwright/test';

// End-to-end spec for the Signals page. Mocks every backend endpoint so
// no real server is needed; Vite dev server on 5173 handles the frontend.
const BASE = 'http://localhost:5173';

test.describe('Signals page', () => {
  test.beforeEach(async ({ page }) => {
    // Seed the Indicators localStorage with one user indicator so the
    // signal has something to reference. Fresh Signals storage.
    await page.addInitScript(() => {
      try {
        // Do NOT clear localStorage here — we rely on it surviving the
        // reload for the persistence test. Each test starts with a fresh
        // Playwright browser context, so state is isolated per-test.
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

    // Mock the discovery endpoints (shared with the Data/Indicators pages).
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

    // Mock the signals-compute endpoint. Return a tiny, deterministic
    // payload so we can assert the chart renders without real data.
    // Shape matches backend `price` contract: {label: "<instrument_id>.<field>",
    // values: [<number|null>, ...]} — NaN-as-null per /api/signals/compute.
    await page.route('**/api/signals/compute', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          index: ['2020-01-02', '2020-01-03', '2020-01-06'],
          position: [0.0, 1.0, 0.0],
          long_score: [0.0, 1.0, 0.0],
          short_score: [0.0, 0.0, 0.0],
          entries_long: [1],
          exits_long: [2],
          entries_short: [],
          exits_short: [],
          // matches backend `price` contract: label = "<instrument_id>.<field>",
          // values may contain nulls where the backend emitted NaN.
          price: { label: '^GSPC.close', values: [3200, 3250, null] },
        }),
      });
    });
  });

  test('renders /signals with three panels and no console errors', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

    await page.goto(`${BASE}/signals`);
    // List header is always present. Scope to main to disambiguate from
    // the identical sidebar nav label.
    await expect(page.getByRole('main').getByText('Signals', { exact: true })).toBeVisible();
    await expect(page.getByTestId('add-signal-btn')).toBeVisible();
    // No signals yet → editor shows the empty state.
    await expect(page.getByText(/Select a signal on the left/)).toBeVisible();

    expect(consoleErrors).toEqual([]);
  });

  test('create a signal, add a block + condition, run, chart renders', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

    await page.goto(`${BASE}/signals`);
    // Create a signal.
    await page.getByTestId('add-signal-btn').click();
    // The block editor should now be visible.
    await expect(page.getByTestId('block-editor')).toBeVisible();
    // All four direction tabs visible.
    await expect(page.getByTestId('direction-tab-long_entry')).toBeVisible();
    await expect(page.getByTestId('direction-tab-long_exit')).toBeVisible();
    await expect(page.getByTestId('direction-tab-short_entry')).toBeVisible();
    await expect(page.getByTestId('direction-tab-short_exit')).toBeVisible();
    // Add a block + condition (default condition is added with the block).
    await page.getByTestId('add-block-btn').click();
    await expect(page.getByTestId('block-0')).toBeVisible();
    await expect(page.getByTestId('condition-0-0')).toBeVisible();
    // Iter-2: operands are unset on a fresh condition, so Run is initially
    // disabled. Pick Constant on both operands to make the condition complete,
    // then Run unlocks.
    // Iter-2: commit a constant on both operand pickers by dispatching
    // click() through evaluate — the horizontal condition-row layout has
    // tight gaps between flex cells that can confuse Playwright's
    // pointer-event hit-testing even when force:true is set.
    const condition = page.getByTestId('condition-0-0');
    await expect(condition.getByTestId('operand-tab-constant')).toHaveCount(2);
    await condition.getByTestId('operand-tab-constant').first().evaluate((el) => el.click());
    await condition.getByTestId('operand-tab-constant').last().evaluate((el) => el.click());
    await expect(page.getByTestId('run-signal-btn')).toBeEnabled();
    await page.getByTestId('run-signal-btn').click();

    // Chart renders — the full-mode test id fires when the response
    // carries a price block.
    await expect(page.getByTestId('signal-chart-full')).toBeVisible({ timeout: 5000 });

    expect(consoleErrors).toEqual([]);
  });

  test('renders position-only chart when backend emits price: null', async ({ page }) => {
    // Override the default mock with a null-price payload to exercise the
    // fallback path end-to-end on the live page.
    await page.route('**/api/signals/compute', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          index: ['2020-01-02', '2020-01-03', '2020-01-06'],
          position: [0.0, 1.0, 0.0],
          long_score: [0.0, 1.0, 0.0],
          short_score: [0.0, 0.0, 0.0],
          entries_long: [1],
          exits_long: [2],
          entries_short: [],
          exits_short: [],
          price: null,
        }),
      });
    });

    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();
    await expect(page.getByTestId('block-editor')).toBeVisible();
    await page.getByTestId('add-block-btn').click();
    await expect(page.getByTestId('condition-0-0')).toBeVisible();
    // Iter-2: pick Constant on both operands to make the condition complete.
    // Iter-2: commit a constant on both operand pickers by dispatching
    // click() through evaluate — the horizontal condition-row layout has
    // tight gaps between flex cells that can confuse Playwright's
    // pointer-event hit-testing even when force:true is set.
    const condition = page.getByTestId('condition-0-0');
    await expect(condition.getByTestId('operand-tab-constant')).toHaveCount(2);
    await condition.getByTestId('operand-tab-constant').first().evaluate((el) => el.click());
    await condition.getByTestId('operand-tab-constant').last().evaluate((el) => el.click());
    await expect(page.getByTestId('run-signal-btn')).toBeEnabled();
    await page.getByTestId('run-signal-btn').click();

    // Position-only testid fires when response carries no price block.
    await expect(page.getByTestId('signal-chart-position-only')).toBeVisible({ timeout: 5000 });
    // Subtitle explains why the price overlay is absent.
    await expect(page.getByTestId('signal-chart-subtitle')).toBeVisible();

    expect(consoleErrors).toEqual([]);
  });

  test('persists signals across reload', async ({ page }) => {
    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();
    // Give the autosave debounce (500 ms) time to flush.
    await page.waitForTimeout(800);
    // Reload — the signal should still be there.
    await page.reload();
    await expect(page.locator('[data-testid^="signal-row-"]').first()).toBeVisible();
  });
});
