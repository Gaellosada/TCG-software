import { test, expect } from '@playwright/test';

// Proactive backend-driven cache indicators (real Chromium, mocked network):
//   - active-config badge "cached" / "not cached",
//   - Compute button reads "Recompute" while cached,
//   - per-row "cached" icon in the saved list,
//   - editing the active config re-probes and flips the badge to "not cached"
//     (invalidation is visible).
//
// The cache-status endpoint is mocked to decide `cached` per query body: a body
// whose active leg weight was edited to 75 is treated as NOT cached; everything
// else is cached. Caching defaults ON (no Settings step needed).

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';

function leg(symbol, weight) {
  return {
    label: symbol, type: 'instrument', collection: 'INDEX', symbol, weight,
    strategy: null, adjustment: null, cycle: null, rollOffset: 0,
    signalId: null, signalName: null, signalSpec: null, option_type: null,
    maturity: null, selection: null, stream: null, roll_offset: null,
    hold_between_rolls: false, nav_times: 1.0,
  };
}
const PF_A = { id: 'pf-a', type: 'portfolio', name: 'Alpha', category: 'RESEARCH', kind: 'pure', locked: false, rebalance: 'none', legs: [leg('SPX', 60)] };
const PF_B = { id: 'pf-b', type: 'portfolio', name: 'Beta', category: 'RESEARCH', kind: 'pure', locked: false, rebalance: 'none', legs: [leg('NDX', 100)] };

async function installRoutes(page) {
  await page.route('**/api/data/collections*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ collections: ['INDEX'] }) }));
  await page.route('**/api/data/INDEX/SPX*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }) }));
  await page.route('**/api/data/INDEX/NDX*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [200, 220] }) }));
  await page.route('**/api/data/INDEX*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }) }));

  await page.route('**/api/persistence/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }));
  await page.route('**/api/persistence/portfolios**', (r) => {
    if (r.request().method() === 'GET') {
      return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([PF_A, PF_B]) });
    }
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PF_A) });
  });

  // The status probe: per-query cached decision.
  await page.route('**/portfolio/cache/status', (r) => {
    const body = JSON.parse(r.request().postData() || '{"queries":[]}');
    const results = (body.queries || []).map((q) => {
      const edited = Object.values(q.weights || {}).some((w) => Number(w) === 75);
      return { cached: !edited };
    });
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ results }) });
  });
}

test('proactive cache badge + Recompute label + per-row icon + edit re-probe flips to not-cached', async ({ page }) => {
  await installRoutes(page);
  await page.goto(`${BASE}/portfolio`);

  // Per-row cache icons render for both saved rows (caching ON by default).
  await expect(page.getByTestId('portfolio-row-cache-pf-a')).toBeVisible();
  await expect(page.getByTestId('portfolio-row-cache-pf-b')).toHaveAttribute('data-cache-status', 'cached');

  // Load Alpha → active config (weight 60) is reported cached.
  await page.getByTestId('load-portfolio-pf-a').click();
  const badge = page.getByTestId('portfolio-cache-badge');
  const compute = page.getByTestId('portfolio-compute-btn');
  await expect(badge).toHaveAttribute('data-cached', 'true');
  await expect(compute).toHaveText('Recompute');

  // Edit the weight to 75 → re-probe → active config is NOT cached.
  await page.locator('input[type="number"]').first().fill('75');
  await expect(badge).toHaveAttribute('data-cached', 'false');
  await expect(compute).toHaveText('Compute');

  // The other (non-active) row stays cached.
  await expect(page.getByTestId('portfolio-row-cache-pf-b')).toHaveAttribute('data-cache-status', 'cached');
});
