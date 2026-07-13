import { test, expect } from '@playwright/test';

// Real-browser round-trip for the portfolio-result cache on the COMPOSED page
// (IndexedDB + SHA-256 body hash that folds in the RESOLVED child spec). Mirrors
// e2e/portfolio-result-cache.spec.js but drives /composed-portfolios: this is the
// authoritative proof that a composed portfolio's key (with its inlined child)
// stores AND retrieves — the gap that unit mocks couldn't cover.
//
// All network mocked via page.route. The observable is the **/portfolio/compute
// hit counter: a cache hit must NOT increment it.

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';

// A pure CHILD (one SPX instrument leg), wire-shaped so the loaded snapshot
// matches legsToWire output.
const CHILD_DOC = {
  id: 'child-1', type: 'portfolio', name: 'Pure Child', category: 'RESEARCH',
  kind: 'pure', locked: false, rebalance: 'none',
  legs: [{
    label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX',
    strategy: null, adjustment: null, cycle: null, rollOffset: 0, weight: 100,
    signalId: null, signalName: null, signalSpec: null, option_type: null,
    maturity: null, selection: null, stream: null, roll_offset: null,
    hold_between_rolls: false, nav_times: 1.0,
  }],
};

// A COMPOSED portfolio referencing that child (a single portfolio-ref leg).
const COMPOSED_DOC = {
  id: 'composed-1', type: 'portfolio', name: 'Composed Cache', category: 'RESEARCH',
  kind: 'composed', locked: false, rebalance: 'none',
  legs: [{ label: 'Block', type: 'portfolio', portfolioId: 'child-1', portfolioName: 'Pure Child', weight: 100 }],
};

const COMPUTE_RESULT = {
  dates: ['2020-01-01', '2020-06-30', '2020-12-31'],
  portfolio_equity: [1, 1.05, 1.1],
  leg_equities: {}, raw_leg_equities: {}, rebalance_dates: [],
  date_range: { start: '2020-01-01', end: '2020-12-31' },
  monthly_returns: [], yearly_returns: [], trades: [], positions: [],
};

async function installRoutes(page) {
  const state = { computeHits: 0 };

  await page.route('**/api/data/collections*', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ collections: ['INDEX'] }),
  }));
  await page.route('**/api/data/INDEX*', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }),
  }));
  // The child's SPX leg range → overlapRange for the composed leg (the fix).
  await page.route('**/api/data/INDEX/SPX*', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }),
  }));

  // Persistence: catch-all first, then the specific portfolios route (last =
  // higher precedence). Detail(child-1) → the pure child; list → the composed.
  await page.route('**/api/persistence/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([]),
  }));
  await page.route('**/api/persistence/portfolios**', (route) => {
    const url = route.request().url();
    if (/\/portfolios\/child-1(\?|$)/.test(url)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CHILD_DOC) });
    }
    if (route.request().method() === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([COMPOSED_DOC]) });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(COMPOSED_DOC) });
  });

  await page.route('**/api/statistics', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({}),
  }));

  await page.route('**/portfolio/compute', (route) => {
    state.computeHits += 1;
    const hits = state.computeHits;
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...COMPUTE_RESULT, date_range: { start: '2020-01-01', end: `20${20 + hits}-12-31` } }),
    });
  });

  return state;
}

async function enableCacheViaSettings(page) {
  await page.goto(`${BASE}/settings`);
  const on = page.getByTestId('portfolio-cache-on');
  await expect(on).toBeVisible();
  await on.click();
  await expect(on).toHaveAttribute('aria-checked', 'true');
}

async function loadComposed(page) {
  const row = page.locator('[data-testid="load-portfolio-composed-1"]');
  await expect(row).toBeVisible();
  await row.click();
  // Editor hydrates the portfolio-ref leg (weight 100).
  await expect(page.locator('input[type="number"]').first()).toHaveValue('100');
}

test('composed page: cache round-trip — badge false→true after compute, persists across reload, hit skipped on cache serve', async ({ page }) => {
  const state = await installRoutes(page);
  await enableCacheViaSettings(page);       // real Settings toggle → localStorage
  await page.goto(`${BASE}/composed-portfolios`); // reload → cache enabled read at mount

  await loadComposed(page);

  const badge = page.getByTestId('portfolio-cache-badge');
  const compute = page.getByTestId('portfolio-compute-btn');
  const dataRange = page.getByText(/Data range:/);

  // The badge RESOLVES (proves currentCacheKey is non-null → child spec + range
  // resolved) and reports NOT cached for a fresh composed config.
  await expect(badge).toBeVisible();
  await expect(badge).toHaveAttribute('data-cached', 'false');
  await expect(dataRange).toHaveCount(0);

  // SEED: one real compute stores the composed result under its content key.
  await compute.click();
  await expect(dataRange).toBeVisible();
  await expect.poll(() => state.computeHits).toBe(1);
  await expect(badge).toHaveAttribute('data-cached', 'true');

  // ── ROUND-TRIP across a full reload (IndexedDB persists) ──
  const seeded = state.computeHits; // 1
  await page.goto(`${BASE}/composed-portfolios`);
  await loadComposed(page);
  await expect(dataRange).toBeVisible();               // auto-displayed from IDB
  await expect(badge).toHaveAttribute('data-cached', 'true');
  await expect(compute).toHaveText('Recompute');        // served from cache
  await page.waitForTimeout(400);                        // let the debounced effect settle
  expect(state.computeHits).toBe(seeded);               // NO new compute on the cache hit
});
