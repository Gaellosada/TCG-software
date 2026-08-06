// End-to-end proof for the per-instrument-data-source option-add → compute flow.
//
// Regression + capability suite for the "Pick a root." bug: adding a 10-delta
// put option leg showed a red "Pick a root." validation and refused to add,
// even after the S&P 500 root was (visually) selected. Root cause: the option
// default was built ONCE from a possibly-empty option-roots list (cold-open
// race / mid-reload after a v1⇄v2 source toggle) → collection '' → NO_ROOT, and
// nothing re-snapped it when roots arrived. With v2's single root (OPT_SP_500)
// the native <select> even *showed* the sole root while React state stayed '',
// and re-picking the already-displayed option fires no change event → the user
// could not repair it by hand. Fix: a self-heal effect in InstrumentPickerModal
// snaps an empty/invalid option collection to a valid root when roots load.
//
// This suite drives the REAL UI against the REAL backend (no compute stubs).
//   Prereqs: backend on :8000 + a Vite dev server proxying /api → :8000.
//   Run:  npx playwright test --config playwright.pi.config.js
//         (TCG_E2E_BASE overrides the default http://localhost:5200)
// Screenshots are written to e2e/__screens__/.
//
// KNOWN DATA-COVERAGE CONSTRAINT (not a bug): data source v2 has NO monthly
// (3rd-Friday) S&P 500 options — only the weekly EW1..EW4 series. A v2 option
// leg must therefore use a weekly cycle (e.g. 'W3 Friday'); a monthly ('M')
// leg is rejected at compute with an explicit backend message. The v2 scenarios
// below pick 'W3 Friday' so they compute; v1 keeps the natural monthly default.

import { test, expect } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = path.join(__dirname, '__screens__');
const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, name), fullPage: true });
// Screenshot centred on the rendered equity curve (scroll it into view first)
// so the "computed chart" evidence unambiguously shows the plotted series.
async function shotChart(page, name) {
  const plot = page.locator('.js-plotly-plot').first();
  await plot.scrollIntoViewIfNeeded();
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(SHOTS, name), fullPage: false });
}

const HOLDINGS = 'table[aria-label="Portfolio holdings"] tbody tr';

// Clear any persisted editor / signals state before each test for isolation.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => { try { window.localStorage.clear(); } catch { /* noop */ } });
});

// Open Add Holding, drill into Options, configure a ~10-delta PUT, confirm.
// `source` ∈ {'v1','v2'}; `cycle` optional wire value (e.g. 'W3 Friday').
// `root` optional collection to select explicitly — needed for v1, whose
// option-roots list is multi-root and defaults to the FIRST root (OPT_BTC),
// so the user picks OPT_SP_500 from the dropdown (a path that always worked).
// Omit `root` for v2: its single root must self-heal on its own (the fix).
async function addTenDeltaPut(page, { source, cycle, root } = {}) {
  await page.getByRole('button', { name: 'Add holding' }).click();
  const srcSel = page.getByTestId('picker-data-source-select');
  await srcSel.waitFor({ state: 'visible', timeout: 15000 });
  if (source) await srcSel.selectOption(source);
  await page.getByTestId('picker-options-toggle').click();
  const form = page.getByTestId('option-stream-form');
  await expect(form).toBeVisible({ timeout: 15000 });
  // THE FIX: the root self-heals to a valid (non-empty) collection with no
  // manual pick — this is what was broken (v2's sole root stayed '').
  await expect(form.locator('select[aria-label="Root"]')).not.toHaveValue('', { timeout: 15000 });
  // Validation must be gone (no "Pick a root.") and Confirm enabled.
  await expect(page.getByTestId('option-stream-validation')).toHaveCount(0);
  if (root) await form.locator('select[aria-label="Root"]').selectOption(root);
  await form.locator('input[type=radio][value=P]').check();
  await form.locator('select[aria-label="Selection criterion"]').selectOption('by_delta');
  await form.locator('input[aria-label="Delta target"]').fill('-0.10');
  if (cycle) await form.locator('select[aria-label="Cycle"]').selectOption(cycle);
  const confirm = page.getByTestId('option-stream-confirm');
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId('option-stream-form')).toHaveCount(0);
}

// Click Compute, wait for the live /portfolio/compute response, return status.
async function compute(page) {
  const respP = page.waitForResponse((r) => r.url().includes('/api/portfolio/compute'), { timeout: 220000 });
  const btn = page.getByTestId('portfolio-compute-btn');
  await expect(btn).toBeEnabled({ timeout: 20000 });
  await btn.click();
  const resp = await respP;
  return resp.status();
}

test('v2: add a 10-delta put (self-heals root) → leg row appears → compute renders an equity curve', async ({ page }) => {
  test.setTimeout(300000);
  await page.goto('/portfolio', { waitUntil: 'networkidle' });
  await addTenDeltaPut(page, { source: 'v2', cycle: 'W3 Friday' });

  const rows = page.locator(HOLDINGS);
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText('OPT_SP_500');
  await expect(page.getByTestId(/^leg-datasource-/)).toContainText(/v2/i);
  await rows.first().locator('input[aria-label^="Weight"]').fill('100');
  await shot(page, '01-v2-put-added.png');

  const status = await compute(page);
  expect(status, 'v2 W3-Friday compute should succeed').toBe(200);
  await expect(page.locator('.js-plotly-plot').first()).toBeVisible({ timeout: 30000 });
  await shotChart(page, '02-v2-put-computed.png');
});

test('v1: add a 10-delta put → leg row appears → compute renders an equity curve', async ({ page }) => {
  test.setTimeout(300000);
  await page.goto('/portfolio', { waitUntil: 'networkidle' });
  await addTenDeltaPut(page, { source: 'v1', root: 'OPT_SP_500' }); // v1 SPX, natural monthly default

  const rows = page.locator(HOLDINGS);
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText('OPT_SP_500');
  await shot(page, '03-v1-put-added.png');

  const status = await compute(page);
  expect(status, 'v1 monthly compute should succeed').toBe(200);
  await expect(page.locator('.js-plotly-plot').first()).toBeVisible({ timeout: 30000 });
  await shot(page, '04-v1-put-computed.png');
});

test('portfolio: a spot (INDEX/SPX) + a continuous future (v1) → add + compute', async ({ page }) => {
  test.setTimeout(300000);
  await page.goto('/portfolio', { waitUntil: 'networkidle' });

  // Spot leg — Indexes group → IND_SP_500.
  await page.getByRole('button', { name: 'Add holding' }).click();
  await page.getByTestId('picker-data-source-select').waitFor({ state: 'visible', timeout: 15000 });
  await page.getByRole('button', { name: /Indexes/ }).click();
  await page.getByText('IND_SP_500', { exact: true }).click();
  await expect(page.locator(HOLDINGS)).toHaveCount(1);

  // Continuous future — Futures group → FUT_SP_500 → Select Continuous Series.
  await page.getByRole('button', { name: 'Add holding' }).click();
  await page.getByTestId('picker-data-source-select').waitFor({ state: 'visible', timeout: 15000 });
  await page.getByRole('button', { name: /Futures/ }).click();
  await page.getByText('FUT_SP_500', { exact: true }).click();
  await page.getByRole('button', { name: 'Select Continuous Series' }).click();
  await expect(page.locator(HOLDINGS)).toHaveCount(2);
  await shot(page, '05-spot-future-added.png');

  const status = await compute(page);
  expect(status, 'spot+future compute should succeed').toBe(200);
  await expect(page.locator('.js-plotly-plot').first()).toBeVisible({ timeout: 30000 });
  await shot(page, '06-spot-future-computed.png');
});

test('HEADLINE: a v1 put + a v2 put in ONE portfolio → compute renders a chart', async ({ page }) => {
  test.setTimeout(360000);
  await page.goto('/portfolio', { waitUntil: 'networkidle' });

  await addTenDeltaPut(page, { source: 'v1', root: 'OPT_SP_500' });                 // v1 SPX monthly
  await addTenDeltaPut(page, { source: 'v2', cycle: 'W3 Friday' });                 // v2 SPX weekly (no monthly in v2)

  const rows = page.locator(HOLDINGS);
  await expect(rows).toHaveCount(2);
  // One leg is v1, the other v2 — assert both source badges are present.
  const badges = page.getByTestId(/^leg-datasource-/);
  await expect(badges).toHaveCount(2);
  await shot(page, '07-mixed-v1v2-added.png');

  const status = await compute(page);
  expect(status, 'mixed v1+v2 compute should succeed').toBe(200);
  await expect(page.locator('.js-plotly-plot').first()).toBeVisible({ timeout: 30000 });
  await shotChart(page, '08-mixed-v1v2-computed.png');
});

test('signals: add a spot input (v1) and an option input (v2) with a chosen source → both add without error', async ({ page }) => {
  test.setTimeout(180000);
  await page.goto('/signals', { waitUntil: 'networkidle' });
  await page.getByTestId('add-signal-btn').click();

  // Ensure the Inputs panel is expanded — a pre-existing signal may have been
  // auto-selected first, leaving the (persistent) panel collapsed.
  const inputsToggle = page.getByTestId('inputs-panel-toggle');
  await expect(inputsToggle).toBeVisible({ timeout: 15000 });
  if ((await inputsToggle.getAttribute('aria-expanded')) === 'false') {
    await inputsToggle.click();
  }

  // Input 0 — spot IND_SP_500, source v1.
  await page.getByTestId('inputs-add-btn').click();
  await page.getByTestId('input-picker-0').click();
  await page.getByTestId('picker-data-source-select').waitFor({ state: 'visible', timeout: 15000 });
  await page.getByRole('button', { name: /Indexes/ }).click();
  await page.getByText('IND_SP_500', { exact: true }).click();
  await expect(page.getByTestId('input-row-0')).toContainText('IND_SP_500');

  // Input 1 — a 10-delta put, source v2 (exercises the self-heal in Signals too).
  await page.getByTestId('inputs-add-btn').click();
  await page.getByTestId('input-picker-1').click();
  const srcSel = page.getByTestId('picker-data-source-select');
  await srcSel.waitFor({ state: 'visible', timeout: 15000 });
  await srcSel.selectOption('v2');
  await page.getByTestId('picker-options-toggle').click();
  const form = page.getByTestId('option-stream-form');
  await expect(form).toBeVisible({ timeout: 15000 });
  await expect(form.locator('select[aria-label="Root"]')).not.toHaveValue('', { timeout: 15000 });
  await expect(page.getByTestId('option-stream-validation')).toHaveCount(0);
  await form.locator('input[type=radio][value=P]').check();
  await form.locator('select[aria-label="Selection criterion"]').selectOption('by_delta');
  await form.locator('input[aria-label="Delta target"]').fill('-0.10');
  const confirm = page.getByTestId('option-stream-confirm');
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.getByTestId('option-stream-form')).toHaveCount(0);
  await expect(page.getByTestId('input-row-1')).toContainText('OPT_SP_500');

  await shot(page, '09-signals-inputs-added.png');
});
