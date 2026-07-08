import { test, expect } from '@playwright/test';

// Real-browser regression for the reported "Save does nothing" bug on the
// Portfolio page (reported TWICE):
//
//   With autosave OFF, after editing a leg the Save button reads SOLID and
//   "Unsaved changes" shows. Clicking Save fired the PUT (data WAS persisted)
//   but the UI never reflected it — the button stayed solid and "Unsaved
//   changes" never cleared, because usePortfolio's ``dirty`` flag was set on
//   every edit but reset only on load/clear, NEVER on save.
//
// This drives the REAL app (real usePortfolio + PortfolioPage + SaveControls +
// useBackendAutosave) in Chromium. Persistence endpoints are mocked via
// page.route so the drive is deterministic and does NOT depend on the (flaky)
// dwh warehouse — exactly the harness pattern the other e2e specs use.

const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';
const OUT = '/home/gael/claude_workspace/trajectoire_cap/TCG-software/.claude/worktrees/fut-notional-sizing/output/screenshots';

// A wire-shaped instrument leg (all fields present) so the loaded snapshot
// matches what legsToWire serializes — mirrors a real round-tripped doc.
const WIRE_LEG = {
  label: 'SPX', type: 'instrument', collection: 'INDEX', symbol: 'SPX',
  strategy: null, adjustment: null, cycle: null, rollOffset: 0, weight: 60,
  signalId: null, signalName: null, signalSpec: null, option_type: null,
  maturity: null, selection: null, stream: null, roll_offset: null,
  hold_between_rolls: false, nav_times: 1.0,
};

const DOC = {
  id: 'pf-drive', type: 'portfolio', name: 'Drive Portfolio',
  category: 'RESEARCH', locked: false, legs: [WIRE_LEG], rebalance: 'none',
};

test('manual Save (autosave OFF) fires the PUT AND clears the dirty UI', async ({ page }) => {
  const putBodies = [];

  // Data discovery — keep the page from erroring; harmless minimal responses.
  await page.route('**/api/data/collections*', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ collections: ['INDEX'] }),
  }));
  await page.route('**/api/data/INDEX*', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ dates: [20200101, 20201231], close: [100, 110] }),
  }));

  // Persistence: GET list vs PUT update distinguished by method.
  await page.route('**/api/persistence/portfolios**', async (route) => {
    const req = route.request();
    const method = req.method();
    if (method === 'PUT') {
      putBodies.push(JSON.parse(req.postData() || '{}'));
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ ...DOC, ...JSON.parse(req.postData() || '{}') }),
      });
    }
    // GET (list). PATCH/POST fall through to a benign 200 too.
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify([DOC]),
    });
  });

  await page.goto(`${BASE}/portfolio`);

  // Select the persisted portfolio → editor hydrates the leg.
  const row = page.locator('[data-testid="load-portfolio-pf-drive"]');
  await expect(row).toBeVisible();
  await row.click();

  const weightInput = page.locator('input[type="number"]').first();
  await expect(weightInput).toHaveValue('60');

  const controls = page.getByTestId('save-controls');
  const saveBtn = controls.getByRole('button', { name: 'Save' });
  // Right after load nothing is dirty — button is clean (transparent).
  await expect(saveBtn).toHaveAttribute('data-clean', 'true');

  // Turn autosave OFF (the reported scenario). It starts checked.
  const autosaveCb = controls.getByRole('checkbox', { name: 'Auto save' });
  await autosaveCb.uncheck();
  await expect(autosaveCb).not.toBeChecked();

  // Edit the leg weight 60 → 75. Dirty: button solid + "Unsaved changes".
  await weightInput.fill('75');
  await expect(saveBtn).toHaveAttribute('data-clean', 'false');
  await expect(page.getByText('Unsaved changes')).toBeVisible();
  await page.screenshot({ path: `${OUT}/before-save.png` });

  const putsBefore = putBodies.length;

  // Click Save.
  await saveBtn.click();

  // (a) The PUT fires with the edited leg — data IS persisted.
  await expect.poll(() => putBodies.length).toBeGreaterThan(putsBefore);
  const body = putBodies[putBodies.length - 1];
  expect(body.legs[0].weight).toBe(75);

  // (b) The UI reflects the save: button goes clean + "Unsaved changes" clears
  //     + the "saved" status shows. THIS is what regressed.
  await expect(saveBtn).toHaveAttribute('data-clean', 'true');
  await expect(page.getByText('Unsaved changes')).toHaveCount(0);
  await expect(page.getByTestId('save-status')).toContainText('saved');
  await page.screenshot({ path: `${OUT}/after-save.png` });

  // (c) Re-editing after a save re-dirties (don't break dirty tracking).
  await weightInput.fill('80');
  await expect(saveBtn).toHaveAttribute('data-clean', 'false');
});
