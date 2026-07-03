import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

// ---------------------------------------------------------------------------
// misc-batch follow-up — VISUAL verification of the width tweaks (asks #1/#2/#4)
// and the Help fire-mode section (ask #3). Read-only: every persistence call is
// mocked (no real app-data DB touched). Screenshots land in the task output dir.
// ---------------------------------------------------------------------------

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';
const OUT_DIR =
  process.env.TCG_SHOT_DIR ||
  '/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/misc-batch-fixes-jul03/output';

// A signal with a THEN chain (links) so the ThenConnector "within [W] bars"
// input renders, an indicator-LHS binary (widened indicator + input selects),
// and an instrument-vs-constant binary (widened instrument field + constant).
const SEEDED_SIGNAL = {
  id: 'sig-widths',
  name: 'Width Check',
  category: 'DEFAULT',
  locked: false,
  description: '',
  inputs: [
    { id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: '^GSPC' } },
  ],
  rules: {
    entries: [
      {
        id: 'blk-1',
        name: 'entry_1',
        input_id: 'X',
        weight: 100,
        enabled: true,
        links: { 1: 10 },
        conditions: [
          {
            op: 'gt',
            lhs: {
              kind: 'indicator',
              indicator_id: 'macd-line',
              input_id: 'X',
              output: 'default',
              params_override: null,
              series_override: null,
            },
            rhs: { kind: 'constant', value: 0 },
          },
          {
            // A cross ×N / within W condition so the N + W inputs render (W is
            // the doubled one).
            op: 'cross_above',
            count: 2,
            window: 30,
            lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
            rhs: { kind: 'constant', value: 42 },
          },
        ],
      },
    ],
    exits: [],
    resets: [],
  },
  settings: { dont_repeat: true },
};

async function mockBackend(page) {
  await page.route('**/api/persistence/signals*', async (route) => {
    const body = route.request().method() === 'GET'
      ? JSON.stringify([SEEDED_SIGNAL])
      : JSON.stringify(SEEDED_SIGNAL);
    await route.fulfill({ status: 200, contentType: 'application/json', body });
  });
  await page.route('**/api/persistence/indicators*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/persistence/portfolios*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/persistence/baskets*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/data/collections*', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ collections: ['INDEX'] }),
    });
  });
  await page.route('**/api/data/INDEX*', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        items: [{ symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' }],
        total: 1, skip: 0, limit: 500,
      }),
    });
  });
  await page.route('**/api/options/**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
}

test.describe('misc width tweaks + Help fire mode', () => {
  test.beforeEach(async ({ page }) => { await mockBackend(page); });

  test('signal block: widened THEN-bars, operand fields, inputs name', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 950 });
    await page.goto(`${BASE}/signals`);

    await expect(page.getByTestId('block-editor')).toBeVisible();
    await expect(page.getByTestId('condition-0-0')).toBeVisible();
    await expect(page.getByTestId('condition-0-1')).toBeVisible();

    // ask #1 — THEN connector "within [W] bars" input, doubled 44 -> 88px.
    const thenBars = page.getByTestId('link-window-0-1');
    await expect(thenBars).toBeVisible();
    const thenBox = await thenBars.boundingBox();
    expect(thenBox.width, `THEN bars input width ${thenBox.width}`).toBeGreaterThanOrEqual(80);

    // ask #2 — indicator name select doubled 73 -> 146px.
    const indSelect = page.getByTestId('operand-indicator-select').first();
    const indBox = await indSelect.boundingBox();
    expect(indBox.width, `indicator select width ${indBox.width}`).toBeGreaterThanOrEqual(130);

    // ask #2 — indicator INPUT select also doubled (same 146 basis).
    const indInput = page.getByTestId('operand-indicator-input').first();
    const indInputBox = await indInput.boundingBox();
    expect(indInputBox.width, `indicator input select ${indInputBox.width}`).toBeGreaterThanOrEqual(130);

    // ask #2 — instrument field lives in a 200px cell (doubled from 100).
    const instrField = page.getByTestId('operand-instrument-field').first();
    const instrBox = await instrField.boundingBox();
    expect(instrBox.width, `instrument field ${instrBox.width}`).toBeGreaterThanOrEqual(150);

    // follow-up — BOTH the cross N (×count) and W (within-bars) inputs doubled
    // 46 -> 92px.
    const crossW = page.getByTestId('cross-window-0-1');
    await expect(crossW).toBeVisible();
    const crossWBox = await crossW.boundingBox();
    expect(crossWBox.width, `cross W input width ${crossWBox.width}`).toBeGreaterThanOrEqual(80);
    const crossN = page.getByTestId('cross-count-0-1');
    const crossNBox = await crossN.boundingBox();
    expect(crossNBox.width, `cross N input width ${crossNBox.width}`).toBeGreaterThanOrEqual(80);

    // ask #4 — inputs-panel id (name) field, doubled 56 -> 112px. The panel is
    // collapsed by default when it already has inputs, so expand it first.
    const idFieldPre = page.getByTestId('input-id-0');
    if (!(await idFieldPre.isVisible())) {
      await page.getByTestId('inputs-panel-toggle').click();
    }
    const idField = page.getByTestId('input-id-0');
    await expect(idField).toBeVisible();
    const idBox = await idField.boundingBox();
    expect(idBox.width, `inputs name field ${idBox.width}`).toBeGreaterThanOrEqual(100);

    // Panel must not overflow horizontally at a normal window.
    const editorPanel = page.locator('[class*="editorPanel"]').first();
    const overflow = await editorPanel.evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(overflow, `editor overflow ${overflow}px`).toBeLessThanOrEqual(1);

    fs.mkdirSync(OUT_DIR, { recursive: true });
    await page.screenshot({ path: path.join(OUT_DIR, 'widths-signal-block.png'), fullPage: false });
  });

  test('help page: fire mode section is its own titled block with an example', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 950 });
    await page.goto(`${BASE}/help`);

    // Jump to the Signals help section.
    await page.getByRole('button', { name: 'Signals' }).click();

    const fire = page.getByText(/Fire mode: pulse vs\. sustained/i);
    await expect(fire).toBeVisible();
    await fire.click(); // expand the <details>
    await expect(page.getByText(/3 taps within 30 bars/i)).toBeVisible();

    // Scroll the fire-mode summary near the top so the expanded example body
    // (pulse/sustained bullets + worked example) is fully in frame.
    await fire.evaluate((el) => el.scrollIntoView({ block: 'start' }));
    await page.mouse.wheel(0, -40);
    fs.mkdirSync(OUT_DIR, { recursive: true });
    await page.screenshot({ path: path.join(OUT_DIR, 'help-fire-mode.png'), fullPage: false });
  });
});
