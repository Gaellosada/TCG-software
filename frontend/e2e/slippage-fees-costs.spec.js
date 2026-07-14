import { test, expect } from '@playwright/test';

// LIVE real-browser smoke for the slippage & fees feature (Statistics "Costs"
// column). dwh is unreachable from the harness, so the /api/portfolio/compute
// endpoint is mocked via page.route (the pattern every portfolio e2e uses) — we
// still drive the REAL Settings→request→Statistics wiring in Chromium:
//   * the Settings bps (localStorage) ride the real compute request body,
//   * the response's total_*_paid_pct render as TWO separate rows,
//   * they render as percents verbatim (0.15 -> "0.15%", NOT "15.00%"),
//   * at 0 bps both rows read "0.00%".
// The "costs actually reduce equity" property is a backend-compute fact proven
// numerically by the Python integration tests (real engine assembly); it cannot
// come from a mocked response.

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
const PF = { id: 'pf-a', type: 'portfolio', name: 'Alpha', category: 'RESEARCH', kind: 'pure', locked: false, rebalance: 'none', legs: [leg('SPX', 100)] };

// A compute response rich enough for the page to mount Statistics. ``totals``
// injects the two cost percents; ``dates``/``portfolio_equity`` satisfy the
// Statistics mount guard (>=2 positive-finite equity points).
function computeResponse({ slippagePct, feesPct }) {
  return {
    dates: ['2020-01-01', '2020-06-01', '2020-12-31'],
    portfolio_equity: [100.0, 105.0, 108.0],
    leg_equities: { SPX: [100.0, 105.0, 108.0] },
    leg_metrics: { SPX: {} },
    metrics: {},
    monthly_returns: [],
    yearly_returns: [],
    rebalance: 'none',
    return_type: 'normal',
    date_range: { start: '2020-01-01', end: '2020-12-31' },
    full_date_range: { start: '2020-01-01', end: '2020-12-31' },
    total_slippage_paid_pct: slippagePct,
    total_fees_paid_pct: feesPct,
    from_cache: false,
    computed_ms: 1,
  };
}

// Installs the market/persistence mocks and a compute stub that ECHOES the cost
// intent: it records the request body and returns totals derived from the bps on
// the wire (10 bps slippage -> 0.15%, 5 bps fees -> 0.075%; absent -> 0). This
// lets one handler serve both the nonzero and the 0-bps flows faithfully.
async function installRoutes(page, captured) {
  await page.route('**/api/data/collections*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ collections: ['INDEX'] }) }));
  await page.route('**/api/data/INDEX*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }) }));
  await page.route('**/portfolio/cache/status', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ results: [{ cached: false }] }) }));
  await page.route('**/api/persistence/portfolios**', (r) => {
    if (r.request().method() === 'GET') {
      return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([PF]) });
    }
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PF) });
  });
  await page.route('**/api/persistence/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }));
  await page.route('**/api/portfolio/compute*', (r) => {
    const body = JSON.parse(r.request().postData() || '{}');
    if (captured) captured.push(body);
    const slip = Number(body.slippage_bps) || 0;
    const fees = Number(body.fees_bps) || 0;
    return r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(computeResponse({ slippagePct: slip * 0.015, feesPct: fees * 0.015 })),
    });
  });
}

// Locate a Costs row's rendered value by its label, within the Costs column.
// Filter to the innermost div containing the exact label, then its value span
// (MetricRow renders <span label/><span value/>).
function costValue(page, label) {
  const row = page
    .getByTestId('statistics-costs')
    .locator('div')
    .filter({ has: page.getByText(label, { exact: true }) })
    .last();
  return row.locator('span').last();
}

async function runComputeWithBps(page, { slippageBps, feesBps }, captured) {
  await installRoutes(page, captured);
  await page.addInitScript(([s, f]) => {
    window.localStorage.setItem('tcg-slippage-bps', String(s));
    window.localStorage.setItem('tcg-fees-bps', String(f));
  }, [slippageBps, feesBps]);
  await page.goto(`${BASE}/portfolio`);
  await page.getByTestId('load-portfolio-pf-a').click();
  await page.getByTestId('portfolio-compute-btn').click();
  await expect(page.getByTestId('statistics-costs')).toBeVisible();
}

test('nonzero bps: Settings bps ride the request; two Costs rows render as percents (no x100)', async ({ page }) => {
  const captured = [];
  await runComputeWithBps(page, { slippageBps: 10, feesBps: 5 }, captured);

  // The real Settings→request wiring put the bps on the wire.
  const withCosts = captured.find((b) => b.slippage_bps || b.fees_bps);
  expect(withCosts, 'compute request carried slippage/fees bps').toBeTruthy();
  expect(Number(withCosts.slippage_bps)).toBe(10);
  expect(Number(withCosts.fees_bps)).toBe(5);

  // Two SEPARATE rows, each labelled, rendered verbatim as percent (0.15%,
  // 0.08%) — NOT the x100-inflated "15.00%"/"7.50%" the 'percent' path would give.
  await expect(costValue(page, 'Slippage paid')).toHaveText('0.15%');
  await expect(costValue(page, 'Fees paid')).toHaveText('0.08%');
});

test('zero bps: both Costs rows read 0.00%', async ({ page }) => {
  await runComputeWithBps(page, { slippageBps: 0, feesBps: 0 });
  await expect(costValue(page, 'Slippage paid')).toHaveText('0.00%');
  await expect(costValue(page, 'Fees paid')).toHaveText('0.00%');
});
