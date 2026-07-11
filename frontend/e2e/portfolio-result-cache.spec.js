import { test, expect } from '@playwright/test';

// Real-browser e2e for the local portfolio-result cache (IndexedDB + SHA-256
// body hash). Drives the REAL app (usePortfolio + PortfolioPage + Settings +
// portfolioCache + computeCacheKey) in Chromium, which has a real IndexedDB —
// this is the authoritative round-trip verification (jsdom has no IndexedDB).
//
// All network is mocked via page.route so the drive is deterministic and does
// NOT touch the dwh warehouse. The crucial mock is **/portfolio/compute, whose
// hit count is the observable that proves cache hits skip the network.

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';
const OUT = '/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/portfolio-result-cache/output';

const WIRE_LEG = {
  label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX',
  strategy: null, adjustment: null, cycle: null, rollOffset: 0, weight: 60,
  signalId: null, signalName: null, signalSpec: null, option_type: null,
  maturity: null, selection: null, stream: null, roll_offset: null,
  hold_between_rolls: false, nav_times: 1.0,
};

const DOC = {
  id: 'pf-cache', type: 'portfolio', name: 'Cache Portfolio',
  category: 'RESEARCH', locked: false, legs: [WIRE_LEG], rebalance: 'none',
};

const COMPUTE_RESULT = {
  dates: ['2020-01-01', '2020-06-30', '2020-12-31'],
  portfolio_equity: [1, 1.05, 1.1],
  leg_equities: {},
  raw_leg_equities: {},
  rebalance_dates: [],
  date_range: { start: '2020-01-01', end: '2020-12-31' },
  monthly_returns: [],
  yearly_returns: [],
  trades: [],
  positions: [],
};

const STATS_RESULT = {
  return: {
    total_return: 0.1, cagr: 0.1, annualized_volatility: 0.15,
    best_day: 0.02, worst_day: -0.02, best_month: 0.05, worst_month: -0.03,
  },
  risk_adjusted: { sharpe_ratio: 0.8, sortino_ratio: 1.1, calmar_ratio: 0.5 },
  tail: { var_95: -0.02, var_99: -0.03, cvar_5: -0.025, skewness: null, kurtosis: null },
  drawdown: {
    max_drawdown: -0.08, avg_drawdown: -0.03, current_drawdown: 0,
    longest_drawdown_days: 30, time_underwater_days: 40,
  },
  risk_free_rate_used: 0.04,
  num_observations: 3,
};

// Register every mock the Portfolio + Settings pages touch and return the
// mutable compute-hit counter.
async function installRoutes(page) {
  const state = { computeHits: 0 };

  // NOTE: Playwright checks routes most-recently-registered FIRST, so general
  // patterns are registered before the specific ones that must win.
  await page.route('**/api/data/collections*', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ collections: ['INDEX'] }),
  }));
  await page.route('**/api/data/INDEX*', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }),
  }));
  // Instrument price series → drives the leg's date range → overlapRange.
  await page.route('**/api/data/INDEX/SPX*', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }),
  }));

  // Persistence: general catch-all first, then the specific portfolios route
  // (registered last → higher precedence) returning our single portfolio.
  await page.route('**/api/persistence/**', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([]),
  }));
  await page.route('**/api/persistence/portfolios**', async (route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify([DOC]),
      });
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...DOC }),
    });
  });

  await page.route('**/api/statistics', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(STATS_RESULT),
  }));

  // THE observable: every real compute call increments the counter.
  await page.route('**/portfolio/compute', (route) => {
    state.computeHits += 1;
    return route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(COMPUTE_RESULT),
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

async function loadPortfolio(page) {
  const row = page.locator('[data-testid="load-portfolio-pf-cache"]');
  await expect(row).toBeVisible();
  await row.click();
  // Editor hydrates the leg (weight 60).
  await expect(page.locator('input[type="number"]').first()).toHaveValue('60');
}

test('cache ON: two computes of an unchanged portfolio hit the compute route exactly once', async ({ page }) => {
  const state = await installRoutes(page);
  await enableCacheViaSettings(page);   // real Settings toggle → localStorage
  await page.goto(`${BASE}/portfolio`); // full reload → usePortfolio reads enabled=true

  await loadPortfolio(page);

  const badge = page.getByTestId('portfolio-cache-badge');
  // Badge appears once the range resolves; before any compute it's "Not cached".
  await expect(badge).toBeVisible();
  await expect(badge).toHaveAttribute('data-cached', 'false');

  const compute = page.getByRole('button', { name: 'Compute', exact: true });

  // First compute → real network call, result renders, entry cached.
  await compute.click();
  await expect(page.getByText(/Data range:/)).toBeVisible();
  await expect.poll(() => state.computeHits).toBe(1);
  // Badge flips to cached after the write.
  await expect(badge).toHaveAttribute('data-cached', 'true');
  await expect(badge).toContainText('Cached');

  // Second compute of the UNCHANGED portfolio → served from cache, ZERO network.
  await compute.click();
  await expect(page.getByText(/Data range:/)).toBeVisible();
  // Give any (erroneous) network call time to land, then assert it did NOT.
  await page.waitForTimeout(300);
  expect(state.computeHits).toBe(1);
  await page.screenshot({ path: `${OUT}/cache-on-hit.png` });

  // Editing the weight changes the body → new key → badge flips to Not cached.
  const weightInput = page.locator('input[type="number"]').first();
  await weightInput.fill('75');
  await expect(badge).toHaveAttribute('data-cached', 'false');

  // Third compute (edited portfolio) → cache miss → route fires again.
  await compute.click();
  await expect.poll(() => state.computeHits).toBe(2);
  await expect(badge).toHaveAttribute('data-cached', 'true');
  await page.screenshot({ path: `${OUT}/cache-on-after-edit.png` });
});

test('cache OFF (default): every compute hits the route and no badge is shown', async ({ page }) => {
  const state = await installRoutes(page);
  await page.goto(`${BASE}/portfolio`); // default: cache disabled

  await loadPortfolio(page);

  // No badge when the feature is off.
  await expect(page.getByTestId('portfolio-cache-badge')).toHaveCount(0);

  const compute = page.getByRole('button', { name: 'Compute', exact: true });

  await compute.click();
  await expect(page.getByText(/Data range:/)).toBeVisible();
  await expect.poll(() => state.computeHits).toBe(1);

  await compute.click();
  await expect.poll(() => state.computeHits).toBe(2);

  await compute.click();
  await expect.poll(() => state.computeHits).toBe(3);
  await page.screenshot({ path: `${OUT}/cache-off.png` });
});
