import { test, expect } from '@playwright/test';

// Iter-3 verification spec:
//   • Compact layout — 3 blocks × 2 conditions fit at 1280×720 without
//     scroll inside the blocks panel.
//   • Equal heights — all interactive elements in a condition row share
//     the same computed height (tolerance ±2 px).
//   • Operand "+" menu & "× with confirm" flow.
//   • Clip banner renders for clipped=true responses.
//   • Multi-instrument subplot rendering.

const BASE = 'http://localhost:5173';

test.describe('iter-3 UI verification', () => {
  test.use({ viewport: { width: 1280, height: 720 } });

  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      try {
        window.localStorage.setItem('tcg.indicators.v1', JSON.stringify({
          version: 1,
          indicators: [
            {
              id: 'sma',
              name: 'SMA',
              code: "def compute(series, window: int = 20):\n    return series['price']",
              doc: '',
              params: { window: 20 },
              seriesMap: { price: { collection: 'INDEX', instrument_id: '^GSPC' } },
              ownPanel: false,
            },
          ],
          defaultState: {},
        }));
      } catch { /* ignore */ }
    });
    await page.route('**/api/data/collections*', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ collections: ['INDEX'] }) });
    });
    await page.route('**/api/data/INDEX*', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ items: [
          { symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' },
          { symbol: '^NDX',  asset_class: 'INDEX', collection: 'INDEX' },
        ], total: 2, skip: 0, limit: 500 }) });
    });
  });

  test('operand + menu installs an operand; × opens confirm; confirm clears', async ({ page }) => {
    await page.goto(`${BASE}/signals`);
    await page.getByTestId('add-signal-btn').click();
    await page.getByTestId('add-block-btn').click();
    await page.getByTestId('add-condition-0').click();
    await expect(page.getByTestId('condition-0-0')).toBeVisible();

    // Two empty operand slots in a binary condition.
    await expect(page.getByTestId('operand-add-btn')).toHaveCount(2);

    // Click the first + to open the menu and pick Constant.
    await page.getByTestId('operand-add-btn').first().click();
    await expect(page.getByTestId('operand-menu')).toBeVisible();
    await page.getByTestId('operand-menu-constant').click();
    await expect(page.getByTestId('operand-menu')).not.toBeVisible();

    // One filled slot (× button) + one still-empty (+ button).
    await expect(page.getByTestId('operand-clear-btn')).toHaveCount(1);
    await expect(page.getByTestId('operand-add-btn')).toHaveCount(1);

    // Clicking × opens ConfirmDialog.
    await page.getByTestId('operand-clear-btn').click();
    await expect(page.getByTestId('confirm-dialog')).toBeVisible();

    // Confirm clears the operand back to empty.
    await page.getByTestId('confirm-dialog-confirm').click();
    await expect(page.getByTestId('confirm-dialog')).not.toBeVisible();
    await expect(page.getByTestId('operand-add-btn')).toHaveCount(2);
  });

  test('clip banner renders when backend emits clipped=true', async ({ page }) => {
    await page.route('**/api/signals/compute', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        timestamps: [1577923200000, 1578009600000, 1578268800000],
        positions: [
          {
            instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
            values: [0, 1, 1],
            clipped_mask: [false, true, true],
            price: { label: '^GSPC.close', values: [3200, 3250, 3300] },
          },
          {
            instrument: { collection: 'INDEX', instrument_id: '^NDX' },
            values: [0, 0, -0.5],
            clipped_mask: [false, false, false],
            price: null,
          },
        ],
        clipped: true,
        diagnostics: {},
      }) });
    });

    // Seed a runnable signal directly via localStorage (v2 shape).
    await page.addInitScript(() => {
      window.localStorage.setItem('tcg.signals.v2', JSON.stringify({
        version: 2,
        signals: [{
          id: 'seeded',
          name: 'Seeded',
          rules: {
            long_entry: [{
              instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
              weight: 0.6,
              conditions: [{
                op: 'gt',
                lhs: { kind: 'constant', value: 1 },
                rhs: { kind: 'constant', value: 0 },
              }],
            }],
            long_exit: [],
            short_entry: [],
            short_exit: [],
          },
        }],
      }));
    });

    await page.goto(`${BASE}/signals`);
    await expect(page.getByTestId('run-signal-btn')).toBeEnabled();
    await page.getByTestId('run-signal-btn').click();

    // The multi-instrument chart stub renders and the clip banner is visible.
    await expect(page.getByTestId('signal-chart-multi')).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('signal-chart-clip-banner')).toBeVisible();
    await expect(page.getByTestId('signal-chart-clip-banner')).toContainText(/clipped/i);
    await expect(page.getByTestId('signal-chart-clip-banner')).toContainText(/\^GSPC/);
  });

  test('compact footprint: 3 blocks × 2 conditions fit at 1280×720 without scroll', async ({ page }) => {
    // Seed 3 blocks × 2 conditions each (all binary), saved to v2 storage.
    await page.addInitScript(() => {
      window.localStorage.setItem('tcg.signals.v2', JSON.stringify({
        version: 2,
        signals: [{
          id: 'compact-demo',
          name: 'Compact',
          rules: {
            long_entry: [1, 2, 3].map(() => ({
              instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
              weight: 0.2,
              conditions: [
                { op: 'gt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 0 } },
                { op: 'lt', lhs: { kind: 'constant', value: 1 }, rhs: { kind: 'constant', value: 2 } },
              ],
            })),
            long_exit: [],
            short_entry: [],
            short_exit: [],
          },
        }],
      }));
    });

    await page.goto(`${BASE}/signals`);
    await expect(page.getByTestId('block-0')).toBeVisible();
    await expect(page.getByTestId('block-1')).toBeVisible();
    await expect(page.getByTestId('block-2')).toBeVisible();

    // Assert the blocks panel shows ALL three block tops within its
    // visible viewport (no scrolling needed to reach block-2).
    const editor = page.locator('[class*="editorPanel"]').first();
    const editorBox = await editor.boundingBox();
    const block2 = page.getByTestId('block-2');
    const block2Box = await block2.boundingBox();
    expect(editorBox).not.toBeNull();
    expect(block2Box).not.toBeNull();
    // block-2 top must sit inside the editor panel's visible region.
    expect(block2Box.y).toBeGreaterThanOrEqual(editorBox.y);
    expect(block2Box.y + block2Box.height).toBeLessThanOrEqual(editorBox.y + editorBox.height + 8);

    // And the scroll position inside the blocks panel is at 0 — nothing
    // had to scroll to make block-2 visible.
    const scrollTop = await editor.evaluate((el) => el.scrollTop);
    expect(scrollTop).toBe(0);
  });

  test('equal-height components in a condition row (±2 px)', async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('tcg.signals.v2', JSON.stringify({
        version: 2,
        signals: [{
          id: 'heights',
          name: 'Heights',
          rules: {
            long_entry: [{
              instrument: { collection: 'INDEX', instrument_id: '^GSPC' },
              weight: 0.5,
              conditions: [{
                op: 'gt',
                lhs: { kind: 'constant', value: 1 },
                rhs: { kind: 'constant', value: 2 },
              }],
            }],
            long_exit: [],
            short_entry: [],
            short_exit: [],
          },
        }],
      }));
    });

    await page.goto(`${BASE}/signals`);
    await expect(page.getByTestId('condition-0-0')).toBeVisible();

    // Collect the heights of all the interactive elements inside the
    // condition row: the op select, the clear-buttons, and the constant
    // inputs. Assert they are within ±2 px of each other.
    const row = page.getByTestId('condition-0-0').locator('[class*="conditionRow"]').first();
    const heights = await row.evaluate((el) => {
      const targets = el.querySelectorAll('button, select, input');
      return Array.from(targets).map((t) => t.getBoundingClientRect().height);
    });
    expect(heights.length).toBeGreaterThanOrEqual(3);
    const min = Math.min(...heights);
    const max = Math.max(...heights);
    expect(max - min).toBeLessThanOrEqual(2);
  });
});
