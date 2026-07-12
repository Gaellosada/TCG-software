import { test, expect } from '@playwright/test';

// Real-browser coverage for the Composed Portfolios feature (design §7):
//   A1-6 — two pages render, share components, nav switches; the pure page has
//          NO "Add portfolio" action while the composed page does; each page's
//          saved list is filtered by ``kind``.
//   A1-5 — a broken portfolio reference (child 404s) shows a broken-ref badge
//          and blocks compute with a clear error (never a crash).
//
// Persistence + market endpoints are mocked via page.route so the drive is
// deterministic and independent of the (flaky) dwh warehouse — the harness
// pattern the other e2e specs use. The /api/portfolio/compute endpoint is
// mocked too and we assert it is NEVER hit on the broken-ref path.

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';

const PURE_DOC = {
  id: 'pure-1', type: 'portfolio', name: 'Pure Block', category: 'RESEARCH',
  kind: 'pure', locked: false, rebalance: 'none',
  legs: [{
    label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX',
    strategy: null, adjustment: null, cycle: null, rollOffset: 0, weight: 100,
    signalId: null, signalName: null, signalSpec: null, option_type: null,
    maturity: null, selection: null, stream: null, roll_offset: null,
    hold_between_rolls: false, nav_times: 1.0,
  }],
};

const COMPOSED_DOC = {
  id: 'composed-1', type: 'portfolio', name: 'Composed Strategy', category: 'RESEARCH',
  kind: 'composed', locked: false, rebalance: 'none',
  legs: [{
    label: 'Block', type: 'portfolio', portfolioId: 'missing-child',
    portfolioName: 'Deleted Child', weight: 100,
  }],
};

// Register the shared market-data + persistence mocks on a page. ``composeHit``
// (optional array) records any /api/portfolio/compute call.
async function installMocks(page, computeHits) {
  await page.route('**/api/data/collections*', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ collections: ['INDEX'] }),
  }));
  await page.route('**/api/data/INDEX*', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }),
  }));
  // Compute — should never fire on the broken-ref path.
  await page.route('**/api/portfolio/compute*', (route) => {
    if (computeHits) computeHits.push(route.request().url());
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ dates: ['2020-01-01'], portfolio_equity: [1] }),
    });
  });
  // Persistence: distinguish the per-id DETAIL (child resolution) from the LIST.
  await page.route('**/api/persistence/portfolios**', (route) => {
    const url = route.request().url();
    // GET /portfolios/missing-child → 404 (deleted child → broken reference).
    if (/\/portfolios\/missing-child(\?|$)/.test(url)) {
      return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'not found' }) });
    }
    if (/\/portfolios\/pure-1(\?|$)/.test(url)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PURE_DOC) });
    }
    // LIST (any category) → both docs; the page filters by kind client-side.
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([PURE_DOC, COMPOSED_DOC]),
    });
  });
}

test('A1-6: two pages share components; pure has no Add Portfolio, composed does; lists filter by kind', async ({ page }) => {
  await installMocks(page);

  // ── Pure page ──
  await page.goto(`${BASE}/portfolio`);
  await expect(page.getByRole('heading', { name: 'Portfolio', exact: true })).toBeVisible();
  // Shared Holdings component present; NO "Add portfolio" action.
  await expect(page.getByRole('button', { name: 'Add holding' })).toBeVisible();
  await expect(page.getByTestId('add-portfolio-btn')).toHaveCount(0);
  // Saved list shows the pure doc, not the composed one.
  await expect(page.getByTestId('load-portfolio-pure-1')).toBeVisible();
  await expect(page.getByTestId('load-portfolio-composed-1')).toHaveCount(0);

  // ── Navigate to the composed page via the sidebar ──
  await page.getByRole('link', { name: 'Composed' }).click();
  await expect(page).toHaveURL(/\/composed-portfolios$/);
  await expect(page.getByRole('heading', { name: 'Composed Portfolios' })).toBeVisible();
  // SAME Holdings component + the composed-only "Add portfolio" action.
  await expect(page.getByRole('button', { name: 'Add holding' })).toBeVisible();
  await expect(page.getByTestId('add-portfolio-btn')).toBeVisible();
  // Saved list now shows the composed doc, not the pure one.
  await expect(page.getByTestId('load-portfolio-composed-1')).toBeVisible();
  await expect(page.getByTestId('load-portfolio-pure-1')).toHaveCount(0);

  // The picker lists ONLY pure portfolios (depth-1 #1).
  await page.getByTestId('add-portfolio-btn').click();
  await expect(page.getByTestId('portfolio-picker')).toBeVisible();
  await expect(page.getByTestId('portfolio-picker-row-pure-1')).toBeVisible();
  await expect(page.getByTestId('portfolio-picker-row-composed-1')).toHaveCount(0);
});

test('A1-5: broken portfolio reference badges the leg and blocks compute (no crash, no compute call)', async ({ page }) => {
  const computeHits = [];
  await installMocks(page, computeHits);

  await page.goto(`${BASE}/composed-portfolios`);
  // Load the composed portfolio whose child (missing-child) 404s.
  await page.getByTestId('load-portfolio-composed-1').click();

  // The portfolio-ref leg renders with a "broken reference" badge once the
  // child resolution fails.
  await expect(page.getByText('broken reference')).toBeVisible();

  // Compute is blocked with a clear error and never hits the backend.
  await page.getByTestId('portfolio-compute-btn').click();
  await expect(page.locator('text=/can.?t be resolved|could not be resolved|deleted, archived, or empty/i')).toBeVisible();
  expect(computeHits).toHaveLength(0);
});
