import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

// ---------------------------------------------------------------------------
// BUG #4 regression spec — Signals condition-block layout at CONSTRAINED
// widths (Tauri webview / narrow desktop). The condition row used
// ``flex-wrap: nowrap`` so at narrow widths the indicator/operand dropdowns
// were crushed below readable width (the squish). This spec seeds a signal
// with the WIDEST condition shapes (an indicator-LHS binary condition + an
// in_range 3-operand condition) and asserts, at several constrained
// viewports, that:
//   (a) the editor panel never overflows horizontally (no clip), and
//   (b) every operand <select> keeps a readable width.
// Screenshots are written to the task output dir for before/after evidence.
//
// Servers are started manually (see playwright.config.js). Mocks every
// backend endpoint the Signals page touches so no real backend is needed.
// ---------------------------------------------------------------------------

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';
const OUT_DIR =
  process.env.TCG_SHOT_DIR ||
  '/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/futures-options-rolling-fixes/output';

// A fully-formed v6 SignalOut doc with a configured spot input and one entry
// block carrying two conditions: a binary (indicator LHS + constant RHS) and
// an in_range (operand + min + max). The indicator is ``macd-line`` (2 params
// → Tier-3 "Parameters" dropdown), the widest operand variant.
const SEEDED_SIGNAL = {
  id: 'sig-layout',
  name: 'Layout Repro',
  category: 'DEFAULT',
  locked: false,
  description: '',
  inputs: [
    {
      id: 'X',
      instrument: { type: 'spot', collection: 'INDEX', instrument_id: '^GSPC' },
    },
  ],
  rules: {
    entries: [
      {
        id: 'blk-1',
        name: 'entry_1',
        input_id: 'X',
        weight: 100,
        enabled: true,
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
            op: 'in_range',
            operand: { kind: 'instrument', input_id: 'X', field: 'close' },
            min: { kind: 'constant', value: 10 },
            max: { kind: 'constant', value: 20 },
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
  // Signals list (TanStack query, the persisted source of truth). Returns a
  // JSON array of SignalOut docs (listSignals resolves res.json() directly).
  await page.route('**/api/persistence/signals*', async (route) => {
    // Only the list GET needs a body; PUT/lock/etc. just succeed.
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([SEEDED_SIGNAL]),
      });
    } else {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(SEEDED_SIGNAL),
      });
    }
  });
  // listIndicators — defaults still provide the indicator list, so an empty
  // array here just exercises the defaults-only fallback (no console error).
  await page.route('**/api/persistence/indicators*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  // Other persistence lists the page may warm.
  await page.route('**/api/persistence/portfolios*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/persistence/baskets*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route('**/api/data/collections*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ collections: ['INDEX'] }),
    });
  });
  await page.route('**/api/data/INDEX*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [{ symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' }],
        total: 1, skip: 0, limit: 500,
      }),
    });
  });
  // InputsPanel warms the options-roots list for the instrument picker. With
  // no backend this 500s and pollutes the console-error assertion, so stub it.
  await page.route('**/api/options/**', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
}

// Viewports to prove reactivity. The Tauri desktop window's minWidth is 1024
// and default width 1440 (desktop/src-tauri/tauri.conf.json), so 1024 is the
// NARROWEST width the packaged app can ever present — the true constrained
// target. 1280 + 1440 cover the default range. (A narrower 900 viewport is
// below the Tauri floor and is exercised separately, sidebar-collapsed, in the
// dedicated test below — it must not overflow the page even if it scrolls.)
const WIDTHS = [1024, 1280, 1440];

test.describe('Signals condition-block layout (BUG #4)', () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page);
  });

  for (const width of WIDTHS) {
    test(`condition blocks reflow cleanly at ${width}px`, async ({ page }) => {
      const consoleErrors = [];
      page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });

      await page.setViewportSize({ width, height: 900 });
      await page.goto(`${BASE}/signals`);

      // The seeded signal auto-selects → BlockEditor renders the Entries tab.
      const editor = page.getByTestId('block-editor');
      await expect(editor).toBeVisible();
      // The seeded block + both conditions are present.
      await expect(page.getByTestId('block-0')).toBeVisible();
      await expect(page.getByTestId('condition-0-0')).toBeVisible();
      await expect(page.getByTestId('condition-0-1')).toBeVisible();
      // The indicator operand's select must be present (filled indicator slot).
      const indSelect = page.getByTestId('operand-indicator-select').first();
      await expect(indSelect).toBeVisible();

      fs.mkdirSync(OUT_DIR, { recursive: true });
      await page.screenshot({
        path: path.join(OUT_DIR, `signals-block-${width}.png`),
        fullPage: false,
      });

      // --- Assertion 1: the editor panel must not overflow horizontally. ----
      // scrollWidth > clientWidth (by > 1px tolerance) ⇒ content is clipped /
      // forces a horizontal scrollbar (the squish symptom).
      const editorPanel = page.locator('[class*="editorPanel"]').first();
      const overflow = await editorPanel.evaluate(
        (el) => el.scrollWidth - el.clientWidth,
      );
      expect(overflow, `editor panel overflows by ${overflow}px at ${width}`).toBeLessThanOrEqual(1);

      // --- Assertion 2: every operand <select> keeps a readable width. ------
      // A squished select collapses far below this; a readable dropdown that
      // shows at least a few characters needs ~60px+. We require >= 60px.
      const selects = page.locator(
        '[data-testid="condition-0-0"] select, [data-testid="condition-0-1"] select',
      );
      const count = await selects.count();
      expect(count).toBeGreaterThan(0);
      for (let i = 0; i < count; i += 1) {
        const box = await selects.nth(i).boundingBox();
        expect(box, `select #${i} has no box at ${width}`).not.toBeNull();
        expect(
          box.width,
          `operand select #${i} squished to ${Math.round(box.width)}px at ${width}`,
        ).toBeGreaterThanOrEqual(60);
      }

      // --- Assertion 3: no row clips its own content (each conditionRow's
      // children must fit within the row + a small tolerance). ---------------
      for (const condId of ['condition-0-0', 'condition-0-1']) {
        const row = page.locator(`[data-testid="${condId}"] [class*="conditionRow"]`).first();
        const rowOverflow = await row.evaluate((el) => el.scrollWidth - el.clientWidth);
        expect(
          rowOverflow,
          `${condId} row content overflows by ${rowOverflow}px at ${width}`,
        ).toBeLessThanOrEqual(1);
      }

      expect(consoleErrors, `console errors at ${width}: ${consoleErrors.join(' | ')}`).toEqual([]);
    });
  }

  test('adding a second condition keeps the layout intact (reactivity)', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 900 });
    await page.goto(`${BASE}/signals`);
    await expect(page.getByTestId('block-0')).toBeVisible();

    // Add a third condition to the block (defaults to a binary gt with empty
    // operands — exercises the empty "+" slot path at narrow width).
    await page.getByTestId('add-condition-0').click();
    await expect(page.getByTestId('condition-0-2')).toBeVisible();

    const editorPanel = page.locator('[class*="editorPanel"]').first();
    const overflow = await editorPanel.evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(overflow, `editor overflows by ${overflow}px after add-condition`).toBeLessThanOrEqual(1);
  });

  // Below the Tauri floor (a 900px window, off-spec). The editor column gets
  // very cramped; the contract is only that the PAGE must not grow a
  // horizontal scrollbar — the editor panel may scroll internally (it is
  // overflow:auto) but content must not spill across the whole window. Proves
  // the min-width:0 shrink behaviour degrades gracefully instead of clipping.
  test('does not overflow the page at a sub-floor 900px width', async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 900 });
    await page.goto(`${BASE}/signals`);
    await expect(page.getByTestId('block-0')).toBeVisible();

    const docOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(docOverflow, `page overflows horizontally by ${docOverflow}px at 900`).toBeLessThanOrEqual(1);

    await page.screenshot({
      path: path.join(OUT_DIR, 'signals-block-900-subfloor.png'),
      fullPage: false,
    });
  });
});
