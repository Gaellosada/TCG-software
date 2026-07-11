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
  // computeDelayMs lets a test hold a compute in flight (simulating slow dwh).
  const state = { computeHits: 0, computeDelayMs: 0 };

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

  // THE observable: every real compute call increments the counter. Each call
  // returns a DISTINCT date_range end year (…202<hits>-12-31) so a test can tell
  // WHICH compute's result is on screen. Optionally delayed to hold it in flight.
  await page.route('**/portfolio/compute', async (route) => {
    state.computeHits += 1;
    const hits = state.computeHits;
    if (state.computeDelayMs) {
      await new Promise((r) => { setTimeout(r, state.computeDelayMs); });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...COMPUTE_RESULT,
        date_range: { start: '2020-01-01', end: `20${20 + hits}-12-31` },
      }),
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

test('cache ON: pre-seeded entry auto-displays with zero clicks/route calls; edit blanks, revert re-shows; Compute is always fresh', async ({ page }) => {
  const state = await installRoutes(page);
  await enableCacheViaSettings(page);   // real Settings toggle → localStorage
  await page.goto(`${BASE}/portfolio`); // full reload → usePortfolio reads enabled=true

  await loadPortfolio(page);

  const badge = page.getByTestId('portfolio-cache-badge');
  const compute = page.getByTestId('portfolio-compute-btn');
  const dataRange = page.getByText(/Data range:/);
  const notice = page.getByTestId('portfolio-recompute-needed');

  // Fresh (not cached) config: no result shown, "recompute needed" notice up.
  await expect(badge).toBeVisible();
  await expect(badge).toHaveAttribute('data-cached', 'false');
  await expect(notice).toBeVisible();
  await expect(dataRange).toHaveCount(0);

  // SEED the cache with one real compute.
  await compute.click();
  await expect(dataRange).toBeVisible();
  await expect.poll(() => state.computeHits).toBe(1);
  await expect(badge).toHaveAttribute('data-cached', 'true');

  // ── AUTO-DISPLAY across a full reload (IndexedDB persists) ──
  const seeded = state.computeHits; // 1
  await page.goto(`${BASE}/portfolio`);
  await loadPortfolio(page);
  // Result appears with ZERO Compute clicks and ZERO new compute-route calls.
  await expect(dataRange).toBeVisible();
  await expect(badge).toHaveAttribute('data-cached', 'true');
  await expect(compute).toHaveText('Recompute'); // relabelled while cached shown
  await page.waitForTimeout(400); // let the debounced effect settle
  expect(state.computeHits).toBe(seeded); // no network for the auto-display
  await page.screenshot({ path: `${OUT}/cache-on-autodisplay.png` });

  // ── BLANK-ON-EDIT ──
  const weightInput = page.locator('input[type="number"]').first();
  await weightInput.fill('75');
  await expect(badge).toHaveAttribute('data-cached', 'false');
  await expect(dataRange).toHaveCount(0);       // display cleared
  await expect(notice).toBeVisible();           // "recompute needed"
  expect(state.computeHits).toBe(seeded);       // editing never hits the network
  await page.screenshot({ path: `${OUT}/cache-on-blank-on-edit.png` });

  // ── EDIT-BACK re-shows from cache (content-addressed), still zero network ──
  await weightInput.fill('60');
  await expect(dataRange).toBeVisible();
  await expect(badge).toHaveAttribute('data-cached', 'true');
  await expect(notice).toHaveCount(0);
  expect(state.computeHits).toBe(seeded);

  // ── COMPUTE = FORCE FRESH (while a cached result is shown) ──
  // Clicking Recompute hits the network even though the config is cached, and
  // must NOT blank / double-clobber the fresh result (race guard).
  await expect(compute).toHaveText('Recompute');
  await compute.click();
  await expect.poll(() => state.computeHits).toBe(seeded + 1); // fresh route call
  await expect(dataRange).toBeVisible();      // result restored, never blanked
  await expect(notice).toHaveCount(0);        // recompute notice never appeared
  await expect(badge).toHaveAttribute('data-cached', 'true');

  // ── RACE: recompute a NEWLY-edited (uncached) config; blank-on-edit must not
  // clobber the in-flight compute, and the fresh result must land. ──
  await weightInput.fill('80');
  await expect(dataRange).toHaveCount(0);      // blanked (80 not cached)
  await expect(badge).toHaveAttribute('data-cached', 'false');
  await compute.click();                       // recompute for 80
  await expect.poll(() => state.computeHits).toBe(seeded + 2);
  await expect(dataRange).toBeVisible();       // fresh result shown, not clobbered
  await expect(badge).toHaveAttribute('data-cached', 'true');
  await page.screenshot({ path: `${OUT}/cache-on-after-edit.png` });
});

test('cache OFF (default): no auto-display, no blank-on-edit, Compute hits every click, no badge', async ({ page }) => {
  const state = await installRoutes(page);
  await page.goto(`${BASE}/portfolio`); // default: cache disabled

  await loadPortfolio(page);

  // No cache UI at all when the feature is off.
  await expect(page.getByTestId('portfolio-cache-badge')).toHaveCount(0);
  await expect(page.getByTestId('portfolio-recompute-needed')).toHaveCount(0);

  const compute = page.getByTestId('portfolio-compute-btn');
  const dataRange = page.getByText(/Data range:/);

  // No auto-display: nothing shown until Compute is clicked.
  await expect(compute).toHaveText('Compute');
  await expect(dataRange).toHaveCount(0);
  await page.waitForTimeout(400);
  expect(state.computeHits).toBe(0);

  // Every click hits the route (no cache serve).
  await compute.click();
  await expect(dataRange).toBeVisible();
  await expect.poll(() => state.computeHits).toBe(1);

  await compute.click();
  await expect.poll(() => state.computeHits).toBe(2);

  // No blank-on-edit on the OFF path: editing leaves the last result in place.
  const weightInput = page.locator('input[type="number"]').first();
  await weightInput.fill('75');
  await page.waitForTimeout(400);
  await expect(dataRange).toBeVisible();       // still shown (OFF = today's behavior)
  await expect(compute).toHaveText('Compute'); // never relabelled

  await compute.click();
  await expect.poll(() => state.computeHits).toBe(3);
  await page.screenshot({ path: `${OUT}/cache-off.png` });
});

// FIX A — edit mid-compute: a compute dispatched for config A must NOT display
// its (fresh) result once the user has edited to config B mid-flight. Uses a
// DELAYED compute so the edit lands while A is in flight, and a MutationObserver
// to detect whether A's distinct result (…2022-12-31) ever reaches the DOM.
test('cache ON: editing mid-compute drops the superseded compute result (never shown for the modified config)', async ({ page }) => {
  const state = await installRoutes(page);
  await enableCacheViaSettings(page);
  await page.goto(`${BASE}/portfolio`);
  await loadPortfolio(page);

  const badge = page.getByTestId('portfolio-cache-badge');
  const compute = page.getByTestId('portfolio-compute-btn');
  const dataRange = page.getByText(/Data range:/);
  const notice = page.getByTestId('portfolio-recompute-needed');
  const weightInput = page.locator('input[type="number"]').first();

  // Seed config A (weight 60) → first compute returns …2021-12-31.
  await compute.click();
  await expect(dataRange).toBeVisible();
  await expect.poll(() => state.computeHits).toBe(1);
  await expect(badge).toHaveAttribute('data-cached', 'true');

  // Watch for the NEXT compute's distinct marker (…2022-12-31) ever hitting the DOM.
  await page.evaluate(() => {
    window.__sawFresh = false;
    const check = () => {
      if (document.body && document.body.innerText.includes('2022-12-31')) {
        window.__sawFresh = true;
      }
    };
    window.__freshObs = new MutationObserver(check);
    window.__freshObs.observe(document.body, { childList: true, subtree: true, characterData: true });
    check();
  });

  // Hold the next compute in flight, click Recompute (for config A), then edit
  // to config B (weight 80) while A is still computing.
  state.computeDelayMs = 900;
  await compute.click();                 // recompute A → will return …2022-12-31
  await weightInput.fill('80');          // edit to config B mid-flight
  await expect(badge).toHaveAttribute('data-cached', 'false'); // B is not cached

  // Let compute A land (900ms) and everything settle.
  await expect.poll(() => state.computeHits, { timeout: 5000 }).toBe(2); // A ran (not aborted)
  await page.waitForTimeout(700);

  // FIX A: A's fresh result (…2022-12-31) must NEVER have been displayed.
  const sawFresh = await page.evaluate(() => window.__sawFresh);
  expect(sawFresh).toBe(false);

  // End state for the modified (uncached) config B: blank + "recompute needed".
  await expect(dataRange).toHaveCount(0);
  await expect(notice).toBeVisible();
  await expect(badge).toHaveAttribute('data-cached', 'false');
  await page.screenshot({ path: `${OUT}/cache-on-edit-mid-compute.png` });

  // A stayed cached (the superseded compute was DROPPED from display but still
  // WRITTEN to the cache — it is valid for config A). Reverting to config A
  // auto-displays that freshest A result (…2022-12-31) with NO new compute.
  await weightInput.fill('60');
  await expect(page.getByText(/2022-12-31/)).toBeVisible();
  await expect(badge).toHaveAttribute('data-cached', 'true');
  expect(state.computeHits).toBe(2);
});
