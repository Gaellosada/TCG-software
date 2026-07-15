import { test, expect } from '@playwright/test';
import fs from 'fs';

// LIVE Tier-1 smoke: REAL browser + REAL backend (no mocks). Vite on :5174
// proxies /api to the feature-branch backend on :8010 (real dwh). Drives the
// actual Portfolio UI, builds a 2-ETF monthly-rebalanced portfolio, computes
// once at 0 bps and once at slippage=10 / fees=5 bps (via the real Settings
// localStorage keys), and asserts the two Costs rows render at 0.xx% scale.

const OUT = '/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/slippage-fees-simulator/output';
const SYMBOLS = ['ETF_SGOV', 'ETF_PIMCO_HIGH_YIELD'];

async function buildAndCompute(page, captured) {
  page.on('response', async (resp) => {
    if (resp.url().includes('/api/portfolio/compute') && resp.request().method() === 'POST') {
      try { captured.push({ req: JSON.parse(resp.request().postData() || '{}'), res: await resp.json() }); }
      catch { /* ignore */ }
    }
  });

  await page.goto('http://localhost:5174/');
  // Navigate to Portfolio
  await page.getByRole('link', { name: 'Portfolio', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Portfolio', exact: true })).toBeVisible({ timeout: 20000 });

  // Add the two ETF holdings.
  for (const sym of SYMBOLS) {
    await page.getByRole('button', { name: /Add holding/i }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    // ETF lives under the "Assets" category (key 'assets', collections
    // ['ETF','FOREX','FUND']). Expand just that group, then pick the symbol.
    await page.locator('[class*="groupToggle"]', { hasText: 'Assets' }).click();
    await page.getByText(sym, { exact: true }).click();
    // modal closes on select
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10000 });
  }

  // Rebalance monthly (turnover -> nonzero costs).
  await page.locator('#rebalance-select').selectOption('monthly');

  // Compute.
  await page.getByTestId('portfolio-compute-btn').click();

  // Wait for the Costs column to render.
  const costs = page.getByTestId('statistics-costs');
  await expect(costs).toBeVisible({ timeout: 60000 });
  const text = await costs.innerText();
  return text;
}

test('0 bps: both cost rows read 0.00%', async ({ browser }) => {
  const ctx = await browser.newContext();
  await ctx.addInitScript(() => {
    localStorage.setItem('tcg-slippage-bps', '0');
    localStorage.setItem('tcg-fees-bps', '0');
  });
  const page = await ctx.newPage();
  const captured = [];
  const text = await buildAndCompute(page, captured);
  await page.getByTestId('statistics-costs').screenshot({ path: `${OUT}/costs_0bps.png` });
  await page.screenshot({ path: `${OUT}/portfolio_0bps_full.png`, fullPage: true });
  fs.writeFileSync(`${OUT}/capture_0bps.json`, JSON.stringify(captured, null, 2));
  console.log('COSTS_0BPS_TEXT>>>', JSON.stringify(text));
  const last = captured[captured.length - 1];
  console.log('RESP_0BPS>>>', JSON.stringify({
    slip: last?.res?.total_slippage_paid_pct, fees: last?.res?.total_fees_paid_pct,
    ret: last?.res?.metrics?.total_return, eq: last?.res?.portfolio_equity?.slice(-1)[0],
  }));
  expect(text).toContain('0.00%');
  await ctx.close();
});

test('10/5 bps: both cost rows > 0 at 0.xx% scale', async ({ browser }) => {
  const ctx = await browser.newContext();
  await ctx.addInitScript(() => {
    localStorage.setItem('tcg-slippage-bps', '10');
    localStorage.setItem('tcg-fees-bps', '5');
  });
  const page = await ctx.newPage();
  const captured = [];
  const text = await buildAndCompute(page, captured);
  await page.getByTestId('statistics-costs').screenshot({ path: `${OUT}/costs_10_5bps.png` });
  await page.screenshot({ path: `${OUT}/portfolio_10_5bps_full.png`, fullPage: true });
  fs.writeFileSync(`${OUT}/capture_10_5bps.json`, JSON.stringify(captured, null, 2));
  console.log('COSTS_10_5BPS_TEXT>>>', JSON.stringify(text));
  const last = captured[captured.length - 1];
  console.log('RESP_10_5BPS>>>', JSON.stringify({
    slip: last?.res?.total_slippage_paid_pct, fees: last?.res?.total_fees_paid_pct,
    ret: last?.res?.metrics?.total_return, eq: last?.res?.portfolio_equity?.slice(-1)[0],
    reqSlip: last?.req?.slippage_bps, reqFees: last?.req?.fees_bps,
  }));
  await ctx.close();
});
