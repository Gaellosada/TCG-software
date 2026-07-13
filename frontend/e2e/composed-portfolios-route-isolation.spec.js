import { test, expect } from '@playwright/test';

// Regression for a state-lifecycle bug: /portfolio and /composed-portfolios
// render the SAME PortfolioPage component at the same tree position, so without
// a distinct per-route `key` React reconciles them as ONE instance and
// usePortfolio's state (legs, persistedId, persistedLocked…) LEAKS across the
// route switch — the carried-over legs also couldn't be removed (a leaked
// persistedLocked disabled the holdings fieldset). The `key="pure"` /
// `key="composed"` fix forces a remount with fresh independent state.
//
// Network mocked via page.route (deterministic, no warehouse).

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';

const SPX_LEG = {
  label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX',
  strategy: null, adjustment: null, cycle: null, rollOffset: 0, weight: 60,
  signalId: null, signalName: null, signalSpec: null, option_type: null,
  maturity: null, selection: null, stream: null, roll_offset: null,
  hold_between_rolls: false, nav_times: 1.0,
};

const PURE_DOC = {
  id: 'pure-1', type: 'portfolio', name: 'Pure One', category: 'RESEARCH',
  kind: 'pure', locked: false, rebalance: 'none', legs: [SPX_LEG],
};
const COMPOSED_DOC = {
  id: 'composed-1', type: 'portfolio', name: 'Composed One', category: 'RESEARCH',
  kind: 'composed', locked: false, rebalance: 'none', legs: [],
};

async function installRoutes(page) {
  await page.route('**/api/data/collections*', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ collections: ['INDEX'] }),
  }));
  await page.route('**/api/data/INDEX*', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }),
  }));
  await page.route('**/api/data/INDEX/SPX*', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }),
  }));
  await page.route('**/api/persistence/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([]),
  }));
  await page.route('**/api/persistence/portfolios**', (route) => {
    const url = route.request().url();
    if (/\/portfolios\/pure-1(\?|$)/.test(url)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PURE_DOC) });
    }
    if (route.request().method() === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([PURE_DOC, COMPOSED_DOC]) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PURE_DOC) });
  });
}

test('legs do NOT leak across /portfolio ↔ /composed-portfolios, and composed legs are removable', async ({ page }) => {
  await installRoutes(page);

  // ── Pure page: load a portfolio with a leg ──
  await page.goto(`${BASE}/portfolio`);
  await page.locator('[data-testid="load-portfolio-pure-1"]').click();
  // The SPX leg is present (weight 60).
  await expect(page.locator('input[type="number"]').first()).toHaveValue('60');

  // ── Navigate to the composed page — legs must NOT carry over ──
  await page.getByRole('link', { name: 'Composed' }).click();
  await expect(page).toHaveURL(/\/composed-portfolios$/);
  // Fresh, independent state: the holdings list is empty.
  await expect(page.getByText('No instruments added.', { exact: false })).toBeVisible();
  await expect(page.locator('input[type="number"]')).toHaveCount(0);

  // ── A leg added on the composed page is REMOVABLE ──
  await page.getByTestId('add-portfolio-btn').click();
  await expect(page.getByTestId('portfolio-picker')).toBeVisible();
  await page.getByTestId('portfolio-picker-row-pure-1').getByRole('button', { name: /Add portfolio/i }).click();
  // Leg row appears (Pure One).
  await expect(page.getByText('Pure One')).toBeVisible();
  await expect(page.locator('input[type="number"]')).toHaveCount(1);

  // Remove it → confirm → gone.
  await page.getByRole('button', { name: /^Remove Pure One$/ }).click();
  await page.getByRole('button', { name: 'Remove', exact: true }).click();
  await expect(page.getByText('No instruments added.', { exact: false })).toBeVisible();
  await expect(page.locator('input[type="number"]')).toHaveCount(0);

  // ── Back to the pure page — also fresh (no composed leftovers) ──
  await page.getByRole('link', { name: 'Portfolio', exact: true }).click();
  await expect(page).toHaveURL(/\/portfolio$/);
  await expect(page.locator('input[type="number"]')).toHaveCount(0);
});
