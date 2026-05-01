// E2E regression for the user-reported "I don't see the progress percents"
// bug. The percentage was rendered but flex:1 on the chart's loading-state
// row let it grow to fill the panel, centring text below the viewport (at
// y=1621 in the failing instance, panel was ~1700px tall). Fixed by
// pinning the row to the top with intrinsic height + padding.

import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:5173';

test('option_stream Computing-state shows the percentage in the visible viewport', async ({ page }) => {
  await page.addInitScript(() => {
    try { window.localStorage.clear(); } catch { /* ignore */ }
  });

  await page.route('**/api/data/collections*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ collections: ['INDEX'] }),
  }));
  await page.route('**/api/data/INDEX*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ items: [{ symbol: 'IND_SP_500', asset_class: 'INDEX', collection: 'INDEX' }], total: 1, skip: 0, limit: 500 }),
  }));
  await page.route('**/api/options/roots*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      roots: [{ collection: 'OPT_SP_500', name: 'SP 500', has_greeks: true, providers: ['IVOLATILITY'], expiration_first: '2005-01-21', expiration_last: '2027-12-19', doc_count_estimated: 0, strike_factor_verified: true, last_trade_date: '2024-12-20' }],
    }),
  }));
  let computeStartedAt = 0;
  await page.route('**/api/indicators/compute', async (r) => {
    computeStartedAt = Date.now();
    await new Promise((res) => setTimeout(res, 4000));
    await r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ dates: [20240620], series: [{ label: 'atm_iv', collection: 'OPT_SP_500', instrument_id: 'stream', close: [0.18] }], values: [0.18] }),
    });
  });
  await page.route('**/api/indicators/progress/*', async (r) => {
    const elapsed = computeStartedAt ? Date.now() - computeStartedAt : 0;
    const frac = Math.min(0.9, elapsed / 4000);
    await r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ done: Math.round(frac * 100), total: 100, fraction: frac }),
    });
  });

  await page.goto(`${BASE}/indicators`);
  await page.waitForLoadState('networkidle');

  const defaultHeader = page.getByTestId('category-default');
  if ((await defaultHeader.getAttribute('data-collapsed')) === 'true') {
    await defaultHeader.click();
  }

  const atm = page.getByText('ATM contract IV', { exact: true });
  await expect(atm).toBeVisible({ timeout: 10000 });
  await atm.locator('..').click({ force: true });

  const runBtn = page.getByRole('button', { name: /Run indicator/i });
  await expect(runBtn).toBeEnabled({ timeout: 5000 });
  await runBtn.click();

  await page.waitForTimeout(1500);

  const chartPanel = page.getByTestId('results-card');
  // Single combined element: "Computing... 37%". Bounding box must lie
  // within the visible viewport so the user actually sees it.
  const computingText = chartPanel.getByText(/Computing\.\.\. \d+%/);
  await expect(computingText).toBeVisible();
  const box = await computingText.boundingBox();
  const viewport = page.viewportSize();
  expect(box).toBeTruthy();
  expect(box.y).toBeLessThan(viewport.height);
  expect(box.y + box.height).toBeGreaterThan(0);
});
