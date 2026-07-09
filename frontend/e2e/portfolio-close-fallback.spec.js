import { test, expect } from '@playwright/test';

// LIVE end-to-end for the option `close` (settlement) stream → mid fallback
// surfacing (Wave 2). Drives the REAL Portfolio UI against the REAL backend +
// dwh. Only the persistence LIST is mocked (to inject a runnable seed portfolio
// so the test does not have to click through AddHoldingModal); the actual
// `/api/portfolio/compute` is NOT mocked — it hits the live backend, which now
// falls back to the row quote-mid when a settlement close is a false-zero/NULL.
//
// The seed is a SHORT ~10Δ SPX put: OPT_SP_500, put, by_delta -0.10, 2-month
// End-of-Month maturity, EOM (monthly) roll, stream = close, held between rolls.
// On the old (pre-fallback) backend the 2021-12-31 and 2022-06-30 roll rows
// rendered an em-dash OPEN price (the settlement close was a false-zero). With
// the fallback they now show a numeric mid value WITH the fallback marker.
//
// Ground truth captured live from this worktree's backend (see frontend_report):
//   2021-12-31 open ≈ 20.125  (≈ 20.1)  open_price_fallback = true
//   2022-06-30 open ≈ 24.375  (≈ 24.4)  open_price_fallback = true
//
// NOTE: this spec expects a Vite dev server proxying `/api` to the worktree's
// backend (the fallback lives on this branch). Point it via TCG_E2E_BASE; the
// task harness boots backend :8010 + Vite :5174 (TCG_BACKEND_PORT=8010).

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';
const OUT =
  '/home/gael/claude_workspace/trajectoire_cap/TCG-software/.claude/worktrees/'
  + 'close-mid-fallback/workspace/tasks/option-close-mid-fallback/output';

// Persisted wire leg (matches PortfolioPage.legsToWire output) for the option
// close leg. loadFromPersisted hydrates these fields verbatim; handleCalculate
// then builds the /compute body (cycle 'M', the EOM maturity, by_delta -0.10,
// stream close, hold_between_rolls true, nav_times 1).
const OPTION_LEG = {
  label: 'OPT_SP_500 P close',
  type: 'option_stream',
  collection: 'OPT_SP_500',
  symbol: null,
  strategy: null,
  adjustment: null,
  cycle: 'M',
  rollOffset: 0,
  weight: 100,
  signalId: null,
  signalName: null,
  signalSpec: null,
  option_type: 'P',
  maturity: { kind: 'end_of_month', offset_months: 2 },
  selection: { kind: 'by_delta', target: -0.1, tolerance: 0.05, strict: false },
  stream: 'close',
  roll_offset: null,
  hold_between_rolls: true,
  nav_times: 1.0,
};

const DOC = {
  id: 'pf-close-fallback',
  type: 'portfolio',
  name: 'Close Fallback Portfolio',
  category: 'RESEARCH',
  locked: false,
  legs: [OPTION_LEG],
  rebalance: 'none',
};

// The two roll dates whose settlement close is a false-zero → mid fallback.
const EXPECTED = [
  { date: '2021-12-31', approx: 20.1, exact: 20.125 },
  { date: '2022-06-30', approx: 24.4, exact: 24.375 },
];

test('option close-stream fallback: former em-dash opens render numeric + marked in the trade log', async ({ page }) => {
  test.setTimeout(180000); // live 5-year option compute over the dwh can be slow.

  // Mock ONLY the persistence LIST (inject the seed). Everything else is live.
  await page.route('**/api/persistence/portfolios**', async (route) => {
    const req = route.request();
    if (req.method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([DOC]),
      });
    }
    // Swallow autosave writes so we never mutate live app-data.
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...DOC, ...JSON.parse(req.postData() || '{}') }),
    });
  });

  // Capture the LIVE compute response to prove the real backend produced the
  // fallback values (and to log observed numbers for the report).
  let computeStatus = null;
  let computeBody = null;
  page.on('response', async (r) => {
    if (r.url().includes('/api/portfolio/compute')) {
      computeStatus = r.status();
      try { computeBody = await r.json(); } catch { /* ignore */ }
    }
  });

  await page.goto(`${BASE}/portfolio`);

  // Load the seeded portfolio → the editor hydrates the option leg.
  const row = page.locator('[data-testid="load-portfolio-pf-close-fallback"]');
  await expect(row).toBeVisible({ timeout: 15000 });
  await row.click();

  // Compute (live). Option-only portfolios default to the 5-year window, which
  // spans both assertion dates — no slider drag needed.
  const computeBtn = page.getByRole('button', { name: 'Compute' });
  await expect(computeBtn).toBeEnabled({ timeout: 15000 });
  await computeBtn.click();

  // Trade log appears once results are in.
  const toggle = page.getByTestId('trade-log-toggle');
  await expect(toggle).toBeVisible({ timeout: 150000 });
  await expect.poll(() => computeStatus, { timeout: 150000 }).toBe(200);
  expect(computeBody, 'no live compute body captured').toBeTruthy();

  // Map the two assertion dates → their roll-row open_bar via the live payload.
  const dates = computeBody.dates || [];
  const trades = computeBody.trades || [];
  const rollByDate = {};
  for (const t of trades) {
    if (!String(t.entry_block_id || '').startsWith('roll:')) continue;
    const d = Number.isInteger(t.open_bar) ? dates[t.open_bar] : null;
    if (d) rollByDate[d] = t;
  }
  // eslint-disable-next-line no-console
  console.log('LIVE compute status', computeStatus, '— roll opens on target dates:',
    JSON.stringify(EXPECTED.map((e) => ({
      date: e.date,
      open_price: rollByDate[e.date]?.open_price,
      open_price_fallback: rollByDate[e.date]?.open_price_fallback,
    }))));

  // Backend precondition: the two dates carry a numeric mid open flagged as fallback.
  for (const e of EXPECTED) {
    const t = rollByDate[e.date];
    expect(t, `no roll row on ${e.date}`).toBeTruthy();
    expect(t.open_price_fallback, `${e.date} open not flagged as fallback`).toBe(true);
    expect(Math.abs(t.open_price - e.exact), `${e.date} open ${t.open_price} != ${e.exact}`)
      .toBeLessThan(0.5);
  }

  // Expand the trade log and assert the FE renders each former-em-dash open as a
  // numeric value WITH the fallback marker on that exact row.
  await toggle.click();
  for (const e of EXPECTED) {
    const bar = rollByDate[e.date].open_bar;
    const trRow = page.locator(`[data-testid="trade-row"][data-open-bar="${bar}"]`);
    await expect(trRow, `row for ${e.date} (bar ${bar}) not rendered`).toHaveCount(1);
    const openCell = trRow.getByTestId('trade-open-price');
    // Numeric mid value shown (NOT an em-dash).
    await expect(openCell).toContainText(String(e.exact));
    await expect(openCell).not.toContainText('—');
    // The subtle fallback marker is present on this open cell.
    await expect(openCell.getByTestId('fallback-mark-open')).toBeVisible();
    // Focused evidence: the exact row showing "<mid> *" for the former em-dash.
    await trRow.scrollIntoViewIfNeeded();
    await trRow.screenshot({ path: `${OUT}/roll-${e.date}-open-marked.png` });
  }

  // STATIC help: the option close-series rows carry the Input hint tooltip.
  const hint = page.getByTestId('input-close-hint').first();
  await expect(hint).toBeVisible();
  await expect(hint).toHaveText('OPT_SP_500 P close');

  await page.screenshot({ path: `${OUT}/trade-log-close-fallback.png`, fullPage: true });
});
