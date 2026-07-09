// E2E regression for the "option-only portfolio can't select dates before
// ~2021" bug (task: option-portfolio-date-floor-fix).
//
// Root cause was a FRONTEND artifact: an option_stream leg's date range was
// never resolved (returned {start:null,end:null}), so an option-only portfolio
// fell back to defaultDateRange() = today-5y (~2021) and the TimeRangeSlider
// clamped the start thumb there. The fix resolves the option leg's REAL
// collection coverage (first..last trade_date) via GET /api/options/coverage,
// so the slider floors at the option collection's true history (~2005 for SPX).
//
// The backend/dwh is NOT reachable in this harness (as with every other e2e
// spec here), so network responses are route-mocked — this exercises the REAL
// React component stack (usePortfolio + PortfolioPage + TimeRangeSlider) in a
// real browser. The coverage mock returns an SPX-like 2005 start; the test
// asserts (a) the slider min floors at 2005 (NOT ~2021), and (b) a Compute run
// sends a pre-2021 start and renders an equity curve over a pre-2021 range.

import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:5173';

const OPT_ROOT = {
  collection: 'OPT_SP_500', name: 'SP 500', has_greeks: true,
  providers: ['IVOLATILITY'], expiration_first: '2005-01-21',
  expiration_last: '2027-12-19', doc_count_estimated: 0,
  strike_factor_verified: true, last_trade_date: '2025-06-30',
  cycles: ['M'],
};

async function mockCommon(page) {
  await page.addInitScript(() => {
    try { window.localStorage.clear(); } catch { /* ignore */ }
  });
  // Category browser data (INDEX collection so the picker renders).
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
    body: JSON.stringify({ roots: [OPT_ROOT] }),
  }));
  // THE FIX under test: option leg resolves its real collection coverage.
  await page.route('**/api/options/coverage*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ root: 'OPT_SP_500', start: '2005-12-01', end: '2025-06-30' }),
  }));
  await page.route('**/api/options/expirations*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ root: 'OPT_SP_500', expirations: ['2006-01-20', '2020-06-19', '2025-06-20'] }),
  }));
  // Implied-leverage readout probe in the hold form.
  await page.route('**/api/options/select*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ contract: { strike: 5000 }, premium_mid: 25 }),
  }));
  // Persistence — empty list; portfolio create/update returns a stable id.
  await page.route('**/api/persistence/portfolios**', async (r) => {
    if (r.request().method() === 'POST') {
      return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'pf-test-1' }) });
    }
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });
  await page.route('**/api/persistence/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([]),
  }));
}

// Build an option-only OPT_SP_500 leg via the Add Holding modal.
async function addOptionLeg(page) {
  await page.getByRole('button', { name: /Add holding/i }).click();
  await page.getByTestId('picker-options-toggle').click();
  const form = page.getByTestId('option-stream-form');
  await expect(form).toBeVisible();
  await expect(form.locator('select[aria-label="Root"]')).toHaveValue('OPT_SP_500', { timeout: 15000 });
  const confirm = page.getByTestId('option-stream-confirm');
  await expect(confirm).toBeEnabled({ timeout: 10000 });
  await confirm.click();
}

test('option-only portfolio: date slider floors at the option collection history (~2005), not ~2021', async ({ page }) => {
  await mockCommon(page);

  let computeBody = null;
  await page.route('**/api/portfolio/compute', async (r) => {
    computeBody = JSON.parse(r.request().postData() || '{}');
    await r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        date_range: { start: computeBody.start, end: computeBody.end },
        dates: ['2006-01-03', '2010-01-04', '2018-01-02'],
        portfolio_equity: [100, 130, 180],
        leg_equities: {}, raw_leg_equities: {}, rebalance_dates: [],
        monthly_returns: [], yearly_returns: [], trades: [], positions: [],
        rebalance: 'none',
      }),
    });
  });

  await page.goto(`${BASE}/portfolio`);
  await page.waitForLoadState('networkidle');

  await addOptionLeg(page);

  // The slider must appear and its MIN label must read 2005 — the option
  // collection's true history — NOT ~2021 (today-5y). This is the core fix.
  const minLabel = page.locator('input[aria-label="Start date"]');
  await expect(minLabel).toBeVisible({ timeout: 15000 });
  // The left-hand timeframe label shows the floor month/year ("Dec 2005") —
  // the option collection's true history, NOT ~2021 (today-5y).
  await expect(page.getByText('Dec 2005', { exact: false }).first()).toBeVisible({ timeout: 15000 });

  // Run a backtest WITHOUT dragging: the effective window defaults to the
  // resolved coverage (2005-12-01 .. 2025-06-30), so the request start is
  // pre-2021 — impossible under the old today-5y floor.
  await page.getByRole('button', { name: /^Compute$/ }).click();

  await expect.poll(() => computeBody?.start, { timeout: 15000 }).toBe('2005-12-01');
  expect(computeBody.end).toBe('2025-06-30');
  expect(Number(computeBody.start.slice(0, 4))).toBeLessThan(2021);

  // The equity curve renders over the pre-2021 range (Data range readout).
  await expect(page.getByText(/Data range: 2005-12-01/)).toBeVisible({ timeout: 15000 });
});

test('mixed option + instrument portfolio still anchors on the instrument overlap', async ({ page }) => {
  await mockCommon(page);
  // Instrument prices for the INDEX leg — a 2015..2019 window narrower than the
  // option coverage, so the overlap (and slider floor) must be 2015, proving
  // the option leg no longer forces today-5y and does not widen past real data.
  // Registered AFTER mockCommon's broader `**/api/data/INDEX*` so this more
  // specific per-instrument prices path wins (Playwright matches most-recent).
  await page.route('**/api/data/INDEX/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ dates: [20150102, 20190101], close: [100, 150] }),
  }));

  let computeBody = null;
  await page.route('**/api/portfolio/compute', async (r) => {
    computeBody = JSON.parse(r.request().postData() || '{}');
    await r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        date_range: { start: computeBody.start, end: computeBody.end },
        dates: ['2015-01-02', '2019-01-01'], portfolio_equity: [100, 150],
        leg_equities: {}, raw_leg_equities: {}, rebalance_dates: [],
        monthly_returns: [], yearly_returns: [], trades: [], positions: [], rebalance: 'none',
      }),
    });
  });

  await page.goto(`${BASE}/portfolio`);
  await page.waitForLoadState('networkidle');

  // Add an INDEX instrument leg (category "Indexes" shows instruments directly).
  await page.getByRole('button', { name: /Add holding/i }).click();
  await page.getByText('Indexes', { exact: true }).click();
  await page.getByText('IND_SP_500', { exact: false }).first().click();
  // Add the option leg.
  await addOptionLeg(page);

  // Overlap floor = instrument start (2015), NOT the wider option coverage and
  // NOT today-5y. A Compute run must send a 2015 start.
  await page.getByRole('button', { name: /^Compute$/ }).click();
  await expect.poll(() => computeBody?.start, { timeout: 15000 }).toBe('2015-01-02');
  expect(Number(computeBody.start.slice(0, 4))).toBeLessThan(2021);
});
