// Wave III real-browser verification (race-free: assert on the API JSON the UI
// actually received via waitForResponse, not the pre-refetch header/view).
import { test, expect } from '@playwright/test';

const OUT = '/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/vix-3m-ratio-roll-blowup/output';
const stats = (arr) => {
  const c = arr.filter((x) => x != null && Number.isFinite(x));
  return { min: Math.min(...c), max: Math.max(...c), n: c.length, allPos: c.every((x) => x > 0) };
};

async function openContinuous(page) {
  await page.goto('/data');
  await page.waitForLoadState('networkidle');
  await page.locator('button:has-text("Futures")').click();
  await page.locator('text=FUT_VIX').first().click();
  await page.locator('text=Continuous Series').first().click();
  await expect(page.locator('.js-plotly-plot')).toBeVisible({ timeout: 20000 });
}

// C1 — Data page: the exploding config is now bounded + positive.
test('C1 Data: front_month all-cycles ratio offset90 bounded', async ({ page }) => {
  test.setTimeout(120000);
  await openContinuous(page);
  await page.locator('label:has-text("Roll strategy") select').selectOption('front_month');
  await page.locator('label:has-text("Adjustment") select').selectOption('ratio');
  const off = page.locator('label:has-text("Roll Offset") input');
  await off.fill('90');
  const [resp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.url().includes('/api/data/continuous/FUT_VIX') &&
        r.url().includes('adjustment=ratio') &&
        r.url().includes('roll_offset=90') &&
        r.status() === 200,
      { timeout: 90000 }
    ),
    off.blur(),
  ]);
  const j = await resp.json();
  const s = stats(j.close);
  console.log('C1 ratio series:', JSON.stringify(s), 'rolls=', j.roll_dates.length);
  expect(Number.isFinite(s.max)).toBe(true);
  expect(s.allPos).toBe(true); // no negatives (sentinel gone)
  expect(s.max).toBeLessThan(1e6); // no 1e9/1e55/1e63 explosion
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/w3_c1_data_ratio.png`, fullPage: true });

  // also show the underlying none series is true VIX-scale (clean, no sentinel)
  await page.locator('label:has-text("Adjustment") select').selectOption('none');
  const [respN] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.url().includes('/api/data/continuous/FUT_VIX') &&
        r.url().includes('adjustment=none') &&
        r.url().includes('roll_offset=90') &&
        r.status() === 200,
      { timeout: 90000 }
    ),
    page.locator('label:has-text("Roll strategy") select').selectOption('front_month'),
  ]);
  const sn = stats((await respN.json()).close);
  console.log('C1 none series:', JSON.stringify(sn));
  expect(sn.allPos).toBe(true);
  expect(sn.max).toBeLessThan(200); // real VIX scale
});

// C2 — Data page: Nth-nearest 3M VIX works, rank input gated to nth_nearest.
test('C2 Data: nth_nearest rank=3 monthly sensible', async ({ page }) => {
  test.setTimeout(120000);
  await openContinuous(page);
  // rank input hidden for front_month
  await expect(page.locator('label:has-text("Rank (Nth contract)")')).toHaveCount(0);
  await page.locator('label:has-text("Roll strategy") select').selectOption('nth_nearest');
  await expect(page.locator('label:has-text("Rank (Nth contract)")')).toBeVisible();
  await page.locator('label:has-text("Rank (Nth contract)") input').fill('3');
  await page.locator('label:has-text("Cycle") select').selectOption('M');
  const [resp] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.url().includes('/api/data/continuous/FUT_VIX') &&
        r.url().includes('strategy=nth_nearest') &&
        r.url().includes('rank=3') &&
        r.url().includes('cycle=M') &&
        r.status() === 200,
      { timeout: 90000 }
    ),
    page.locator('label:has-text("Adjustment") select').selectOption('none'),
  ]);
  const j = await resp.json();
  const s = stats(j.close);
  console.log('C2 nth3 series:', JSON.stringify(s), 'rank_echo=', j.rank, 'rolls=', j.roll_dates.length);
  expect(j.rank).toBe(3);
  expect(s.allPos).toBe(true);
  expect(s.min).toBeGreaterThan(8);
  expect(s.max).toBeLessThan(90); // ~3M VIX scale
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/w3_c2_data_nth3.png`, fullPage: true });
});

// Add a FUT_VIX continuous leg via the Portfolio picker, Compute, and return the
// /api/portfolio/compute JSON the UI received.
// The picker is a category browser: expand the FUTURES category, then click the
// FUT_VIX row (a CategoryBrowser entry, not a <button>).
async function pickFutVix(page) {
  await page.getByText('FUTURES', { exact: false }).first().click();
  await page.getByText('FUT_VIX', { exact: true }).first().click({ timeout: 15000 });
}

async function computePortfolioLeg(page, { strategy, adjustment, cycle, offset, rank }) {
  await page.goto('/portfolio');
  await page.waitForLoadState('networkidle');
  await page.locator('button:has-text("Add Holding")').click();
  await pickFutVix(page);
  await expect(page.locator('[data-testid="continuous-spec-picker-strategy"]')).toBeVisible({ timeout: 15000 });
  await page.locator('[data-testid="continuous-spec-picker-strategy"]').selectOption(strategy);
  if (strategy === 'nth_nearest' && rank != null) {
    // rank is a number <input> in the modal (not a <select>) → fill, not selectOption
    await page.locator('[data-testid="continuous-spec-picker-rank"]').fill(String(rank));
  }
  await page.locator('[data-testid="continuous-spec-picker-adjustment"]').selectOption(adjustment);
  await page.locator('[data-testid="continuous-spec-picker-cycle"]').selectOption(cycle);
  const off = page.locator('[data-testid="continuous-spec-picker-roll-offset"]');
  await off.fill(String(offset));
  await page.locator('button:has-text("Select Continuous Series")').click();
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/api/portfolio/compute') && r.status() === 200, { timeout: 90000 }),
    page.locator('button:has-text("Compute")').click(),
  ]);
  return resp.json();
}

// C1 Portfolio — single-leg exploding config → equity bounded & positive.
test('C1 Portfolio: front_month all-cycles ratio offset90 equity bounded', async ({ page }) => {
  test.setTimeout(150000);
  const j = await computePortfolioLeg(page, { strategy: 'front_month', adjustment: 'ratio', cycle: '', offset: 90 });
  const s = stats(j.portfolio_equity);
  console.log('C1 portfolio equity:', JSON.stringify(s));
  expect(Number.isFinite(s.max)).toBe(true);
  expect(s.allPos).toBe(true);
  expect(s.max).toBeLessThan(1e6);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/w3_c1_portfolio.png`, fullPage: true });
});

// C2 Portfolio — nth_nearest rank=3 monthly → equity bounded & sensible.
test('C2 Portfolio: nth_nearest rank=3 monthly equity sensible', async ({ page }) => {
  test.setTimeout(150000);
  const j = await computePortfolioLeg(page, { strategy: 'nth_nearest', adjustment: 'none', cycle: 'M', offset: 0, rank: 3 });
  const s = stats(j.portfolio_equity);
  console.log('C2 portfolio equity:', JSON.stringify(s));
  expect(s.allPos).toBe(true);
  expect(s.max).toBeLessThan(1e5);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/w3_c2_portfolio.png`, fullPage: true });
});

// C3a — Portfolio picker OFFERS Nth-nearest (allowNthNearest=true).
test('C3a gating: Portfolio picker offers nth_nearest', async ({ page }) => {
  test.setTimeout(120000);
  await page.goto('/portfolio');
  await page.waitForLoadState('networkidle');
  await page.locator('button:has-text("Add Holding")').click();
  await pickFutVix(page);
  const strat = page.locator('[data-testid="continuous-spec-picker-strategy"]');
  await expect(strat).toBeVisible({ timeout: 15000 });
  const opts = await strat.locator('option').allInnerTexts();
  console.log('Portfolio strategy options:', opts.join(' | '));
  expect(opts.join(' ')).toContain('Nth-nearest');
  await page.screenshot({ path: `${OUT}/w3_c3_portfolio_has_nth.png` });
});

// C3b — Signals picker does NOT offer Nth-nearest (allowNthNearest defaults false).
test('C3b gating: Signals picker omits nth_nearest', async ({ page }) => {
  test.setTimeout(120000);
  await page.goto('/signals');
  await page.waitForLoadState('networkidle');
  // expand Inputs panel, add a row, open its instrument picker
  const toggle = page.locator('[data-testid="inputs-panel-toggle"]');
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
  await page.locator('[data-testid="inputs-add-btn"]').click();
  await page.locator('[data-testid="input-picker-0"]').click();
  await page.locator('button:has-text("FUT_VIX")').first().click({ timeout: 15000 });
  const strat = page.locator('[data-testid="continuous-spec-picker-strategy"]');
  await expect(strat).toBeVisible({ timeout: 15000 });
  const opts = await strat.locator('option').allInnerTexts();
  console.log('Signals strategy options:', opts.join(' | '));
  expect(opts.join(' ')).not.toContain('Nth-nearest');
  await page.screenshot({ path: `${OUT}/w3_c3_signals_no_nth.png` });
});
