// E2E regression for the user-reported "I don't see the progress" bug.
// The earlier polling-based percentage was rendered but pushed off-
// screen by a flex layout bug, then proved fragile in real use (stuck
// at 0% when the backend wasn't ticking). The progress feature was
// replaced with a simple FE-side elapsed-time badge ("Computing... Ns")
// that mounts only while the parent reports loading.
//
// This spec asserts the badge is visible inside the chart panel during
// a slow option_stream compute and that it ticks past 0s.

import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:5173';

test('option_stream Computing-state shows an elapsed-time badge inside the viewport', async ({ page }) => {
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
  await page.route('**/api/indicators/compute', async (r) => {
    await new Promise((res) => setTimeout(res, 3000));
    await r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ dates: [20240620], series: [{ label: 'atm_iv', collection: 'OPT_SP_500', instrument_id: 'stream', close: [0.18] }], values: [0.18] }),
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

  // Badge must be visible inside the chart panel and reflect elapsed
  // wall-time (1s+ after we wait 1.5s).
  await page.waitForTimeout(1500);

  const chartPanel = page.getByTestId('results-card');
  const badge = chartPanel.getByText(/Computing\.\.\. \d+s/);
  await expect(badge).toBeVisible();

  const box = await badge.boundingBox();
  const viewport = page.viewportSize();
  expect(box).toBeTruthy();
  expect(box.y).toBeLessThan(viewport.height);

  const text = await badge.textContent();
  const seconds = Number(text.match(/(\d+)s/)[1]);
  expect(seconds).toBeGreaterThanOrEqual(1);
});
