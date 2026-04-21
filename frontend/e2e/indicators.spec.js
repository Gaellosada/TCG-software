import { test, expect } from '@playwright/test';

// End-to-end spec for the Indicators page. Uses route mocking so no
// backend is required. Assumes Vite dev server is running on 5173 (same
// convention as sibling specs).
const BASE = 'http://localhost:5173';

test.describe('Indicators page', () => {
  test.beforeEach(async ({ page }) => {
    // Fresh localStorage for every test so persisted state from a previous
    // run does not bleed into the next.
    await page.addInitScript(() => {
      try { window.localStorage.clear(); } catch { /* ignore */ }
    });

    // Mock the discovery endpoints the Indicators page reuses from the
    // Data page.
    await page.route('**/api/data/collections*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ collections: ['INDEX', 'VOL'] }),
      });
    });
    await page.route('**/api/data/INDEX*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            { symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' },
            { symbol: 'NDX', asset_class: 'INDEX', collection: 'INDEX' },
          ],
          total: 2,
          skip: 0,
          limit: 500,
        }),
      });
    });
    await page.route('**/api/data/VOL*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            { symbol: '^VIX', asset_class: 'INDEX', collection: 'VOL' },
          ],
          total: 1,
          skip: 0,
          limit: 500,
        }),
      });
    });
    // The compute endpoint now echoes the label per series row.
    await page.route('**/api/indicators/compute', async (route) => {
      const req = route.request();
      const postData = req.postDataJSON() || {};
      const labels = Object.keys(postData.series || {});
      const series = labels.map((label) => {
        const ref = postData.series[label];
        return {
          label,
          collection: ref.collection,
          instrument_id: ref.instrument_id,
          close: [100.0, 101.0, 102.0],
        };
      });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          dates: ['2024-01-01', '2024-01-02', '2024-01-03'],
          series,
          indicator: [null, null, 101.0],
        }),
      });
    });
  });

  test('four panels present + Run enabled with default indicator seeded', async ({ page }) => {
    await page.goto(`${BASE}/indicators`);
    await page.waitForLoadState('networkidle');

    // 1) + New button lives inside the CUSTOM section header.
    const newBtn = page.getByRole('button', { name: /New indicator/i });
    await expect(newBtn).toBeVisible();

    // 2) Search input present.
    await expect(page.getByPlaceholder('Search indicators...')).toBeVisible();

    // 3) Code editor present (CodeMirror root).
    await expect(page.locator('.cm-editor').first()).toBeVisible();

    // 4) The default indicator row is visible (badge was retired in
    //    iter-7 — its category lives under the DEFAULT section header
    //    instead). The code panel has the readonly overlay applied.
    await expect(page.getByText('SMA', { exact: true })).toBeVisible();
    await expect(page.locator('[data-readonly="true"]')).toHaveCount(1);

    // 5) Run button exists and is enabled (default's ``price`` slot was
    //    auto-populated from the SPX resolver).
    const runBtn = page.getByRole('button', { name: /Run indicator/i });
    await expect(runBtn).toBeVisible();
    await expect(runBtn).toBeEnabled();

    // 6) SaveControls + name input moved to the TOP of the right
    //    (params) column in iter-8. Assert the save-controls bar and
    //    the name input now live INSIDE the params panel (third grid
    //    column), not the editor panel.
    await expect(page.getByTestId('save-controls')).toBeVisible();
    await expect(page.getByLabel('Auto save')).toBeVisible();
    await expect(page.getByLabel('Indicator name')).toBeVisible();
    // The params-panel-top-bar wraps SaveControls + the name input.
    // Use CSS to assert containment: both must be descendants of the
    // 3rd grid column element (paramsPanel). The grid-column-3 element
    // is identifiable via CSS selector.
    const paramsPanelContainsName = await page.evaluate(() => {
      const name = document.querySelector('[aria-label="Indicator name"]');
      const save = document.querySelector('[data-testid="save-controls"]');
      if (!name || !save) return false;
      // Walk up from the name input; at some point the closest ancestor
      // should have grid-column style '3' OR a computed gridColumnStart
      // equal to 3. Simplest: both must share the same ancestor that
      // has aria-label or class containing 'params'.
      const paramsPanel = name.closest('[class*="paramsPanel"]');
      return paramsPanel && paramsPanel.contains(save);
    });
    expect(paramsPanelContainsName).toBe(true);

    // 7) The readonly overlay wraps the CodeMirror editor incl. gutters —
    //    both the gutters and the overlay must exist inside the same
    //    [data-readonly="true"] wrapper.
    await expect(page.locator('[data-readonly="true"] .cm-gutters')).toHaveCount(1);

    // 8) DEFAULT + CUSTOM category headers visible by default (search empty).
    await expect(page.getByTestId('category-default')).toBeVisible();
    await expect(page.getByTestId('category-custom')).toBeVisible();

    // 9) Chart area is wrapped in the shared Card component.
    await expect(page.getByTestId('results-card')).toBeVisible();
  });

  test('category headers hide when the search box has text', async ({ page }) => {
    await page.goto(`${BASE}/indicators`);
    await page.waitForLoadState('networkidle');
    await expect(page.getByTestId('category-default')).toBeVisible();
    await expect(page.getByTestId('category-custom')).toBeVisible();

    await page.getByPlaceholder('Search indicators...').fill('sma');
    await expect(page.getByTestId('category-default')).toHaveCount(0);
    await expect(page.getByTestId('category-custom')).toHaveCount(0);
  });

  test('structured compute error renders styled error panel with traceback + Copy', async ({ page }) => {
    // Override the compute mock with a runtime error envelope.
    await page.route('**/api/indicators/compute', async (route) => {
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({
          error_type: 'runtime',
          message: 'ZeroDivisionError: division by zero',
          traceback: 'Traceback (most recent call last):\n  File "<user>", line 2, in compute\n    return 1/0\nZeroDivisionError: division by zero',
        }),
      });
    });

    await page.goto(`${BASE}/indicators`);
    await page.waitForLoadState('networkidle');

    // Grant clipboard for the Copy button.
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']).catch(() => {});

    await page.getByRole('button', { name: /Run indicator/i }).click();

    // Heading matches the runtime error type (updated in ux-fix: "Indicator error").
    await expect(page.getByRole('heading', { name: /Indicator error/i })).toBeVisible();
    await expect(page.getByText(/ZeroDivisionError: division by zero/).first()).toBeVisible();

    // Traceback is in a collapsible details block.
    await expect(page.locator('details >> summary', { hasText: /traceback/i })).toBeVisible();

    // Copy button is present.
    await expect(page.getByRole('button', { name: /Copy error details/i })).toBeVisible();
  });

  test('clicking DEFAULT header collapses items; clicking again expands', async ({ page }) => {
    await page.goto(`${BASE}/indicators`);
    await page.waitForLoadState('networkidle');
    // First default indicator ("SMA") is visible initially.
    await expect(page.getByText('SMA', { exact: true })).toBeVisible();
    const header = page.getByTestId('category-default');
    await expect(header).toHaveAttribute('data-collapsed', 'false');
    // Collapse.
    await header.click();
    await expect(header).toHaveAttribute('data-collapsed', 'true');
    await expect(page.getByText('SMA', { exact: true })).toHaveCount(0);
    // Re-expand.
    await header.click();
    await expect(header).toHaveAttribute('data-collapsed', 'false');
    await expect(page.getByText('SMA', { exact: true })).toBeVisible();
  });

  test('classified banner appears when navigator.onLine is false', async ({ page }) => {
    // Force offline via init script BEFORE the page loads. The resolver
    // will see ``navigator.onLine === false`` and the classifier should
    // return kind='offline'. We also make listCollections throw to
    // ensure the fetch path errors — otherwise the mocked route would
    // still succeed.
    await page.addInitScript(() => {
      Object.defineProperty(window.navigator, 'onLine', {
        configurable: true,
        get: () => false,
      });
    });
    // Override the collections mock to simulate a network failure.
    await page.route('**/api/data/collections*', async (route) => {
      await route.abort('internetdisconnected');
    });
    await page.goto(`${BASE}/indicators`);
    await page.waitForLoadState('networkidle');
    // The banner should carry the 'offline' kind and the classified copy.
    const banner = page.locator('[data-banner-kind="offline"]');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(/offline/i);
  });

  test('multi-series: adding a second label renders a second picker', async ({ page }) => {
    await page.goto(`${BASE}/indicators`);
    await page.waitForLoadState('networkidle');

    // Create a new user indicator, then replace its code with a two-label variant.
    await page.getByRole('button', { name: /New indicator/i }).click();
    await page.locator('.cm-editor .cm-content').first().click();

    // CodeMirror responds to page.keyboard events — select all, type.
    await page.keyboard.press('Control+A');
    const multiCode = `def compute(series, window: int = 5):\n    p = series['price']\n    v = series['vix']\n    return p - v`;
    await page.keyboard.type(multiCode);

    // The right panel should now show two series slots: price + vix.
    await expect(page.locator('text=price').first()).toBeVisible();
    await expect(page.locator('text=vix').first()).toBeVisible();

    // Two instrument picker buttons mount — one per series label.
    const pickers = page.locator('[data-testid^="instrument-picker-"]');
    await expect(pickers).toHaveCount(2);
  });
});
