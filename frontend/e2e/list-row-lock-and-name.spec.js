import { test, expect } from '@playwright/test';

// End-to-end spec for the list-row lock-on-left + full-name-at-rest change
// applied to the three persisted-entity lists: Signals, Indicators, Portfolio.
//
// Contract under test (for every list, at a normal width AND the 1024px Tauri
// floor):
//   (i)   lock-on-left   — the [data-testid="lock-toggle-btn"] box x is LESS
//                          than the name element's x (lock precedes name).
//   (ii)  idle full name — the name is NOT truncated at rest
//                          (scrollWidth <= clientWidth + 1).
//   (iii) hover reveal    — the .rowActions wrapper width goes from ~0 (rest)
//                          to >0 (hover); the name may crop then.
//   (iv)  no h-overflow   — documentElement.scrollWidth <= innerWidth, idle AND
//                          hover, at both widths.
//
// No backend process is needed — every /api call is mocked via page.route.
// Follows the harness pattern of signals.spec.js (addInitScript + route).
const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';

// Names sized to the narrow Signals/Indicators left panel (~187px content
// width): long enough that they WOULD crop under the old bug (name box shrinks
// by the ~70px the always-present actions reserved → ~117px ≈ 16 chars), but
// SHORT enough to fit the full ~187px panel at rest now that the actions
// collapse to zero width. The Portfolio "Saved Portfolios" card spans the full
// page width, so its name can be much longer.
const LONG_SIGNAL = 'Momentum Breakout Signal';            // ~24 chars
const LONG_INDICATOR = 'Long EMA Crossover Ind';            // ~22 chars (monospace)
const LONG_PORTFOLIO = 'Diversified Multi Asset Growth Portfolio With Long Name';

const WIDTHS = [
  { label: '1280', width: 1280, height: 900 },
  { label: '1024', width: 1024, height: 800 }, // Tauri webview floor
];

// ---- shared measurement helpers (run in the page) --------------------------

// Returns { lockX, nameX, nameScrollW, nameClientW } for a given row + name el.
async function measureRow(page, rowSel, nameSel) {
  return page.evaluate(({ rowSel, nameSel }) => {
    const row = document.querySelector(rowSel);
    if (!row) return null;
    const lock = row.querySelector('[data-testid="lock-toggle-btn"]');
    const name = row.querySelector(nameSel);
    const lockBox = lock ? lock.getBoundingClientRect() : null;
    const nameBox = name ? name.getBoundingClientRect() : null;
    return {
      lockX: lockBox ? lockBox.x : null,
      nameX: nameBox ? nameBox.x : null,
      nameScrollW: name ? name.scrollWidth : null,
      nameClientW: name ? name.clientWidth : null,
    };
  }, { rowSel, nameSel });
}

// The .rowActions wrapper is the direct parent of the action control. Measure
// its rendered width (robust against hashed CSS-module class names).
async function actionsWidth(page, rowSel, actionSel) {
  return page.evaluate(({ rowSel, actionSel }) => {
    const row = document.querySelector(rowSel);
    if (!row) return null;
    const ctl = row.querySelector(actionSel);
    if (!ctl) return null;
    const wrapper = ctl.parentElement; // .rowActions
    return wrapper.getBoundingClientRect().width;
  }, { rowSel, actionSel });
}

async function noHorizontalOverflow(page) {
  return page.evaluate(() => {
    const docOverflow = document.documentElement.scrollWidth - window.innerWidth;
    return { docScrollW: document.documentElement.scrollWidth, innerW: window.innerWidth, docOverflow };
  });
}

// ---------------------------------------------------------------------------
// Signals list — rows come from GET /api/persistence/signals?category=RESEARCH
// ---------------------------------------------------------------------------
test.describe('Signals list — lock-left + full name', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      try {
        window.localStorage.removeItem('tcg.signals.v2');
        window.localStorage.removeItem('tcg.signals.v5');
        window.localStorage.setItem('tcg.signals.autosave', 'false');
      } catch { /* ignore */ }
    });
    // Discovery endpoints the Signals page touches.
    await page.route('**/api/data/collections*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ collections: ['INDEX'] }),
    }));
    await page.route('**/api/data/INDEX*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ items: [{ symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' }], total: 1, skip: 0, limit: 500 }),
    }));
    // The persisted signals list — two signals, one with a very long name.
    await page.route('**/api/persistence/signals*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 'sig-long', name: LONG_SIGNAL, category: 'RESEARCH', locked: false,
          inputs: [], rules: { entries: [], exits: [], resets: [] }, settings: { dont_repeat: true }, doc: '',
        },
        {
          id: 'sig-short', name: 'EMA', category: 'RESEARCH', locked: false,
          inputs: [], rules: { entries: [], exits: [], resets: [] }, settings: { dont_repeat: true }, doc: '',
        },
      ]),
    }));
  });

  for (const vp of WIDTHS) {
    test(`@${vp.label} lock-left, full name at rest, hover reveals actions, no overflow`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(`${BASE}/signals`);

      const rowSel = '[data-testid="signal-row-sig-long"]';
      await expect(page.locator(rowSel)).toBeVisible();
      await page.waitForTimeout(250); // let the row settle / transitions idle

      // (i) lock-on-left + (ii) idle full name (measure at rest, before hover).
      const rest = await measureRow(page, rowSel, '[class*="rowName"]');
      expect(rest, 'row + name measured').not.toBeNull();
      expect(rest.lockX, 'lock x present').not.toBeNull();
      expect(rest.lockX, 'lock precedes name (lock-on-left)').toBeLessThan(rest.nameX);
      expect(rest.nameScrollW, 'name not truncated at rest').toBeLessThanOrEqual(rest.nameClientW + 1);

      // (iii) hover reveal — actions wrapper ~0 at rest, >0 on hover.
      const wRest = await actionsWidth(page, rowSel, '[data-testid="signal-cat-select-sig-long"]');
      expect(wRest, 'actions collapsed at rest (~0)').toBeLessThanOrEqual(1);
      await page.locator(rowSel).hover();
      await page.waitForTimeout(250); // wait out the max-width transition
      const wHover = await actionsWidth(page, rowSel, '[data-testid="signal-cat-select-sig-long"]');
      expect(wHover, 'actions revealed on hover (>0)').toBeGreaterThan(10);
      await expect(page.locator(`${rowSel} [data-testid="signal-cat-select-sig-long"]`)).toBeVisible();

      // (iv) no horizontal overflow — idle was implied by the rest measure; assert on hover too.
      const ov = await noHorizontalOverflow(page);
      expect(ov.docOverflow, `no doc h-overflow on hover @${vp.label}`).toBeLessThanOrEqual(1);

      await page.screenshot({ path: `/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/list-row-lock-and-name/output/screenshots/signals-${vp.label}-hover.png` });
      // Move away and screenshot the idle state too.
      await page.mouse.move(0, 0);
      await page.waitForTimeout(250);
      await page.screenshot({ path: `/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/list-row-lock-and-name/output/screenshots/signals-${vp.label}-idle.png` });
    });
  }
});

// ---------------------------------------------------------------------------
// Indicators list — custom indicators come from localStorage tcg.indicators.v1
// (+ a mocked empty backend list). DEFAULT built-ins load from code on mount.
// ---------------------------------------------------------------------------
test.describe('Indicators list — lock-left + full name', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      try { window.localStorage.setItem('tcg.indicators.autosave', 'false'); } catch { /* ignore */ }
    });

    await page.route('**/api/data/collections*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ collections: ['INDEX'] }),
    }));
    await page.route('**/api/data/INDEX*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ items: [{ symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' }], total: 1, skip: 0, limit: 500 }),
    }));
    // Backend persisted indicators list (the source of CUSTOM rows). Two custom
    // indicators, one with a (panel-fitting) long name. Doc shape matches
    // unpackBackendIndicator: { id, name, locked, definition: {...} }.
    const code = "def compute(series, window: int = 5):\n    return series['close']";
    await page.route('**/api/persistence/indicators*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([
        { id: 'ind-long', name: LONG_INDICATOR, locked: false, definition: { code, doc: '', params: { window: 5 }, seriesMap: {}, ownPanel: false } },
        { id: 'ind-short', name: 'RSI', locked: false, definition: { code, doc: '', params: { window: 5 }, seriesMap: {}, ownPanel: false } },
      ]),
    }));
  });

  for (const vp of WIDTHS) {
    test(`@${vp.label} lock-left, full name at rest, hover reveals actions, no overflow`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(`${BASE}/indicators`);

      // The custom indicator row carries no testid; locate it by its (unique)
      // long name and walk up to the role=button row.
      await expect(page.getByText(LONG_INDICATOR)).toBeVisible();
      await page.waitForTimeout(250);

      // Build a stable selector for THIS row via a data attribute we set here.
      await page.evaluate((longName) => {
        const span = Array.from(document.querySelectorAll('[class*="rowName"]'))
          .find((el) => el.textContent === longName);
        const row = span && span.closest('[role="button"]');
        if (row) row.setAttribute('data-e2e-row', 'ind-long');
      }, LONG_INDICATOR);
      const rowSel = '[data-e2e-row="ind-long"]';
      await expect(page.locator(rowSel)).toBeVisible();

      // (i) + (ii)
      const rest = await measureRow(page, rowSel, '[class*="rowName"]');
      expect(rest, 'row + name measured').not.toBeNull();
      expect(rest.lockX, 'lock x present').not.toBeNull();
      expect(rest.lockX, 'lock precedes name (lock-on-left)').toBeLessThan(rest.nameX);
      expect(rest.nameScrollW, 'name not truncated at rest').toBeLessThanOrEqual(rest.nameClientW + 1);

      // (iii) hover reveal — use the rename ✎ button as the action probe.
      const wRest = await actionsWidth(page, rowSel, 'button[aria-label^="Rename"]');
      expect(wRest, 'actions collapsed at rest (~0)').toBeLessThanOrEqual(1);
      await page.locator(rowSel).hover();
      await page.waitForTimeout(250);
      const wHover = await actionsWidth(page, rowSel, 'button[aria-label^="Rename"]');
      expect(wHover, 'actions revealed on hover (>0)').toBeGreaterThan(10);

      // (iv) no overflow on hover.
      const ov = await noHorizontalOverflow(page);
      expect(ov.docOverflow, `no doc h-overflow on hover @${vp.label}`).toBeLessThanOrEqual(1);

      await page.screenshot({ path: `/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/list-row-lock-and-name/output/screenshots/indicators-${vp.label}-hover.png` });
      await page.mouse.move(0, 0);
      await page.waitForTimeout(250);
      await page.screenshot({ path: `/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/list-row-lock-and-name/output/screenshots/indicators-${vp.label}-idle.png` });
    });
  }
});

// ---------------------------------------------------------------------------
// Portfolio persisted list — backend-backed. Rows come from
// GET /api/persistence/portfolios?category=RESEARCH (mocked).
// ---------------------------------------------------------------------------
test.describe('Portfolio persisted list — lock-left + full name', () => {
  test.beforeEach(async ({ page }) => {
    // The Portfolio page hits a few discovery + compute endpoints; mock the
    // ones that matter and let the rest 404 harmlessly (the list is what we test).
    await page.route('**/api/data/collections*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ collections: ['INDEX'] }),
    }));
    await page.route('**/api/persistence/portfolios*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([
        { id: 'pf-long', name: LONG_PORTFOLIO, category: 'RESEARCH', locked: false, legs: [], rebalance: 'none' },
        { id: 'pf-short', name: 'Core', category: 'RESEARCH', locked: false, legs: [], rebalance: 'none' },
      ]),
    }));
  });

  for (const vp of WIDTHS) {
    test(`@${vp.label} lock-left, full name at rest, hover reveals actions, no overflow`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(`${BASE}/portfolio`);

      const rowSel = '[data-testid="persisted-portfolio-row-pf-long"]';
      await expect(page.locator(rowSel)).toBeVisible();
      await page.waitForTimeout(250);

      // Name here is a <button> (.rowName) — measure it directly.
      const nameSel = '[data-testid="load-portfolio-pf-long"]';
      const rest = await measureRow(page, rowSel, nameSel);
      expect(rest, 'row + name measured').not.toBeNull();
      expect(rest.lockX, 'lock x present').not.toBeNull();
      expect(rest.lockX, 'lock precedes name (lock-on-left)').toBeLessThan(rest.nameX);
      expect(rest.nameScrollW, 'name not truncated at rest').toBeLessThanOrEqual(rest.nameClientW + 1);

      // (iii) hover reveal — probe via the category select.
      const wRest = await actionsWidth(page, rowSel, '[data-testid="portfolio-cat-select-pf-long"]');
      expect(wRest, 'actions collapsed at rest (~0)').toBeLessThanOrEqual(1);
      await page.locator(rowSel).hover();
      await page.waitForTimeout(250);
      const wHover = await actionsWidth(page, rowSel, '[data-testid="portfolio-cat-select-pf-long"]');
      expect(wHover, 'actions revealed on hover (>0)').toBeGreaterThan(10);
      await expect(page.locator(`${rowSel} [data-testid="archive-portfolio-pf-long"]`)).toBeVisible();

      // (iv) no overflow on hover.
      const ov = await noHorizontalOverflow(page);
      expect(ov.docOverflow, `no doc h-overflow on hover @${vp.label}`).toBeLessThanOrEqual(1);

      await page.screenshot({ path: `/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/list-row-lock-and-name/output/screenshots/portfolio-${vp.label}-hover.png` });
      await page.mouse.move(0, 0);
      await page.waitForTimeout(250);
      await page.screenshot({ path: `/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/list-row-lock-and-name/output/screenshots/portfolio-${vp.label}-idle.png` });
    });
  }
});
