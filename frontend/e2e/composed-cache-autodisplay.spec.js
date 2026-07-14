import { test, expect } from '@playwright/test';

// Real-browser coverage for cache auto-display + switching robustness (the
// cache-consistency work). Network is mocked via page.route (the harness pattern
// the other e2e specs use), so the drive is deterministic and dwh-independent.
//
// Asserts:
//   (b) selecting a CACHED portfolio auto-displays its result — /cache/get is
//       called, /compute is NEVER called;
//   (c) an edit that changes the key BLANKS the chart, flips the badge to "not
//       cached", and Compute then re-fires /compute;
//   (a) a COMPOSED portfolio of cached children auto-displays instantly via
//       /cache/get with NO /compute call (fund-of-funds reuse, FE-observable),
//       and the saved list reads "Saved Composed Portfolios".

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

const PURE_A = { id: 'pf-a', type: 'portfolio', name: 'Alpha', category: 'RESEARCH', kind: 'pure', locked: false, rebalance: 'none', legs: [leg('SPX', 60)] };
const PURE_B = { id: 'pf-b', type: 'portfolio', name: 'Beta', category: 'RESEARCH', kind: 'pure', locked: false, rebalance: 'none', legs: [leg('NDX', 100)] };
const COMPOSED = {
  id: 'cmp-1', type: 'portfolio', name: 'FoF', category: 'RESEARCH', kind: 'composed', locked: false, rebalance: 'none',
  legs: [
    { label: 'A', type: 'portfolio', portfolioId: 'pf-a', portfolioName: 'Alpha', weight: 50 },
    { label: 'B', type: 'portfolio', portfolioId: 'pf-b', portfolioName: 'Beta', weight: 50 },
  ],
};

const RESULT_BLOB = {
  dates: ['2020-01-01', '2020-12-31'],
  portfolio_equity: [100, 110],
  leg_equities: {}, raw_leg_equities: {}, rebalance_dates: [],
  date_range: { start: '2020-01-01', end: '2020-12-31' },
  monthly_returns: [], yearly_returns: [], trades: [], positions: [],
  from_cache: true, computed_ms: null,
};

// A body counts as "edited/uncached" when any weight was changed to 75.
function isEdited(body) {
  return Object.values(body.weights || {}).some((w) => Number(w) === 75);
}

async function installMocks(page, counters) {
  // NOTE: Playwright gives precedence to the LAST-registered matching route, so
  // general globs are registered FIRST and specific ones AFTER (so they win).
  await page.route('**/api/data/collections*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ collections: ['INDEX'] }) }));
  await page.route('**/api/data/INDEX*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }) }));
  await page.route('**/api/data/INDEX/SPX*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }) }));
  await page.route('**/api/data/INDEX/NDX*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [200, 220] }) }));

  // General persistence catch-all FIRST; the portfolios-specific route AFTER so
  // it takes precedence (the list + per-id detail must not fall through to []).
  await page.route('**/api/persistence/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }));
  await page.route('**/api/persistence/portfolios**', (r) => {
    const url = r.request().url();
    if (/\/portfolios\/pf-a(\?|$)/.test(url)) return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PURE_A) });
    if (/\/portfolios\/pf-b(\?|$)/.test(url)) return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PURE_B) });
    if (/\/portfolios\/cmp-1(\?|$)/.test(url)) return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(COMPOSED) });
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([PURE_A, PURE_B, COMPOSED]) });
  });

  // Status probe: cached unless edited-to-75.
  await page.route('**/portfolio/cache/status', (r) => {
    const body = JSON.parse(r.request().postData() || '{"queries":[]}');
    const results = (body.queries || []).map((q) => ({ cached: !isEdited(q) }));
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ results }) });
  });

  // Read-only cache-get: HIT (blob) unless edited-to-75; NEVER computes.
  await page.route('**/portfolio/cache/get', (r) => {
    if (counters) counters.get += 1;
    const body = JSON.parse(r.request().postData() || '{}');
    const payload = isEdited(body)
      ? { result: null, from_cache: false }
      : { result: RESULT_BLOB, from_cache: true };
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) });
  });

  // Compute: record every hit; must be zero on the auto-display paths.
  await page.route('**/portfolio/compute', (r) => {
    if (counters) counters.compute += 1;
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...RESULT_BLOB, from_cache: false, computed_ms: 5 }) });
  });
}

test('(b/c) select cached auto-displays (no compute); edit blanks + flips badge; Compute re-fires', async ({ page }) => {
  const counters = { get: 0, compute: 0 };
  await installMocks(page, counters);
  await page.goto(`${BASE}/portfolio`);

  // Load Alpha → its cached result auto-displays with NO Compute click.
  await page.getByTestId('load-portfolio-pf-a').click();
  await expect(page.getByText(/Data range:/)).toBeVisible();
  expect(counters.get).toBeGreaterThan(0);   // /cache/get was used
  expect(counters.compute).toBe(0);          // auto-display NEVER computes

  const badge = page.getByTestId('portfolio-cache-badge');
  await expect(badge).toHaveAttribute('data-cached', 'true');

  // Edit the weight to 75 → chart blanks, badge flips to not-cached.
  await page.locator('input[type="number"]').first().fill('75');
  await expect(page.getByText(/Data range:/)).toHaveCount(0);
  await expect(badge).toHaveAttribute('data-cached', 'false');
  expect(counters.compute).toBe(0);          // editing still never computes

  // Compute now re-fires the compute route and shows the result.
  await page.getByTestId('portfolio-compute-btn').click();
  await expect(page.getByText(/Data range:/)).toBeVisible();
  expect(counters.compute).toBe(1);
});

test('(a) composed portfolio of cached children auto-displays instantly via /cache/get, no /compute', async ({ page }) => {
  const counters = { get: 0, compute: 0 };
  await installMocks(page, counters);
  await page.goto(`${BASE}/composed-portfolios`);

  // FIX 3: the composed route's saved list heading.
  await expect(page.getByText('Saved Composed Portfolios')).toBeVisible();

  // Load the composed portfolio → auto-displays from cache; no compute at all.
  await page.getByTestId('load-portfolio-cmp-1').click();
  await expect(page.getByText(/Data range:/)).toBeVisible();
  expect(counters.get).toBeGreaterThan(0);
  expect(counters.compute).toBe(0);
});
