import { test, expect } from '@playwright/test';

// E2E for Wave-1b: exit-block multi-target dropdowns must all share the SAME
// width (= the bottom row's), with "+ Add block" kept to the right of the
// bottom dropdown. jsdom can't measure layout, so the real verification is a
// bounding-box width comparison here.
//
// We pre-seed a fully-formed signal (2 named entry blocks + 1 exit block
// targeting BOTH) via the mocked persistence list — SignalsPage hydrates
// `rules.entries`/`rules.exits` straight off the doc (hydrateFromPersisted),
// and the list is the source of rows. No backend process needed.
const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';

const SIGNAL_DOC = {
  id: 'sig-exit',
  name: 'Exit Width Signal',
  category: 'RESEARCH',
  locked: false,
  inputs: [{ id: 'X', instrument: { type: 'spot', collection: 'INDEX', instrument_id: '^GSPC' } }],
  rules: {
    entries: [
      { id: 'e1', name: 'Alpha', input_id: 'X', weight: 10, conditions: [] },
      { id: 'e2', name: 'Beta', input_id: 'X', weight: -5, conditions: [] },
    ],
    // One exit block closing BOTH entries → two target dropdown rows.
    exits: [
      { id: 'x1', name: 'ExitAll', target_entry_block_names: ['Alpha', 'Beta'], conditions: [] },
    ],
    resets: [],
  },
  settings: { dont_repeat: true },
  description: '',
};

const WIDTHS = [
  { label: '1280', width: 1280, height: 900 },
  { label: '1024', width: 1024, height: 800 }, // Tauri webview floor
];

test.describe('Exit-block multi-target dropdown widths', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      try {
        window.localStorage.removeItem('tcg.signals.v2');
        window.localStorage.removeItem('tcg.signals.v5');
        window.localStorage.setItem('tcg.signals.autosave', 'false');
      } catch { /* ignore */ }
    });
    await page.route('**/api/data/collections*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ collections: ['INDEX'] }),
    }));
    await page.route('**/api/data/INDEX*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ items: [{ symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' }], total: 1, skip: 0, limit: 500 }),
    }));
    // The signals list — one fully-formed signal with a 2-target exit block.
    await page.route('**/api/persistence/signals*', (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([SIGNAL_DOC]),
    }));
  });

  for (const vp of WIDTHS) {
    test(`@${vp.label} both target dropdowns are equal width; add button right of bottom row`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.goto(`${BASE}/signals`);

      // The seeded signal auto-selects (first/only in the list). Open Exits.
      await expect(page.getByText('Exit Width Signal')).toBeVisible();
      await page.getByTestId('section-tab-exits').click();

      const sel0 = page.getByTestId('target-entry-select-0-0');
      const sel1 = page.getByTestId('target-entry-select-0-1');
      await expect(sel0).toBeVisible();
      await expect(sel1).toBeVisible();
      // Confirm the seeded targets came through.
      await expect(sel0).toHaveValue('Alpha');
      await expect(sel1).toHaveValue('Beta');

      // The real "+ Add block" is unique (clone carries no testid).
      const addBtn = page.getByTestId('add-target-0');
      await expect(addBtn).toBeVisible();
      await page.waitForTimeout(150); // settle layout

      // (1) The two target dropdowns have EQUAL bounding-box width (±1px).
      const b0 = await sel0.boundingBox();
      const b1 = await sel1.boundingBox();
      expect(b0, 'row0 dropdown box').not.toBeNull();
      expect(b1, 'row1 dropdown box').not.toBeNull();
      expect(Math.abs(b0.width - b1.width), `equal dropdown widths @${vp.label} (got ${b0.width} vs ${b1.width})`).toBeLessThanOrEqual(1);

      // (2) "+ Add block" sits to the RIGHT of the bottom (row 1) dropdown, on
      // the same row (not relocated below the list).
      const addBox = await addBtn.boundingBox();
      expect(addBox.x, 'add button is right of the bottom dropdown').toBeGreaterThan(b1.x + b1.width - 1);
      // Vertically overlaps the bottom row (same line), NOT below the list.
      const addCenterY = addBox.y + addBox.height / 2;
      expect(addCenterY, 'add button vertically within the bottom row').toBeGreaterThan(b1.y - 2);
      expect(addCenterY, 'add button vertically within the bottom row').toBeLessThan(b1.y + b1.height + 2);

      // (3) No horizontal overflow at this width.
      const ov = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(ov, `no doc h-overflow @${vp.label}`).toBeLessThanOrEqual(1);

      await page.screenshot({ path: `/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/list-row-lock-and-name/output/screenshots/exit-targets-${vp.label}.png` });
    });
  }
});
