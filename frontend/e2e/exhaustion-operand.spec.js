import { test, expect } from '@playwright/test';

// Reproduction spec for the POST-DELIVERY ISSUE: user reports they "can't use
// a compare operand" with the two new default indicators (Exhaustion, NthTap).
//
// This drives the EXACT user flow in a real (mocked-backend) browser:
//   1. open /signals, create a signal with one configured input + a block
//      carrying a Compare condition,
//   2. on an operand slot pick Indicator -> assert the dropdown contains
//      `exhaustion` and `nthtap`, select exhaustion, assert its param panel
//      renders,
//   3. set the comparator to `eq` and enter -1 in the constant operand
//      (prime suspect for the bug), then verify `eq 1` and `ge N`,
//   4. Run (mocked /api/signals/compute) and assert results render.
//
// All backend endpoints are mocked so no live server is required.
const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';
const OUT = '/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/indicator-stateful-helpers/output';

// A backend SignalOut payload (the list endpoint shape): one configured spot
// input (X -> INDEX/^GSPC) and one ENTRY block bound to X (weight +1 => long)
// with a single `gt` Compare condition whose operands are still null. This
// lets the test focus purely on the operand flow without driving instrument
// selection through the data UI. The signals list loads from the backend, so
// this is injected by mocking GET /api/signals?category=RESEARCH.
const SEED_SIGNAL_DOC = {
  id: 'sig-exh',
  name: 'Exhaustion Test',
  category: 'RESEARCH',
  locked: false,
  description: '',
  inputs: [{
    id: 'X',
    instrument: { type: 'spot', collection: 'INDEX', instrument_id: '^GSPC' },
  }],
  rules: {
    entries: [{
      id: 'b1',
      name: 'Block 1',
      input_id: 'X',
      weight: 1,
      enabled: true,
      conditions: [{ op: 'gt', lhs: null, rhs: null }],
      description: '',
    }],
    // An entry-only signal cannot run (positions never close); the Run-gate
    // requires at least one exit. This exit targets Block 1 with a complete,
    // already-runnable condition so the ONLY thing the test configures is the
    // entry block's Exhaustion operand.
    exits: [{
      id: 'x1',
      name: 'Exit 1',
      target_entry_block_names: ['Block 1'],
      enabled: true,
      conditions: [{
        op: 'lt',
        lhs: { kind: 'instrument', input_id: 'X', field: 'close' },
        rhs: { kind: 'constant', value: 0 },
      }],
      description: '',
    }],
    resets: [],
  },
  settings: {},
};

test.describe('Exhaustion / NthTap as a Compare operand (repro)', () => {
  test.beforeEach(async ({ page }) => {
    // Signals list loads from the backend — mock it to inject our seed doc.
    await page.route('**/api/persistence/signals?*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([SEED_SIGNAL_DOC]),
      });
    });
    // Autosave / lock writes — swallow so they don't 500 against the proxy.
    await page.route('**/api/persistence/signals/**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SEED_SIGNAL_DOC) });
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
    // No user indicators — defaults only (mirrors the real defaults-only path).
    // listIndicators() returns the body directly as an array.
    await page.route('**/api/persistence/indicators*', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
    });
    // Options roots probe (fired by the input panel) — unrelated to this
    // feature; stub it so it doesn't 500 against the unreachable proxy.
    await page.route('**/api/options/**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ roots: [] }) });
    });

    await page.route('**/api/signals/compute', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          timestamps: [1577923200000, 1578009600000, 1578268800000],
          positions: [{
            input_id: 'X',
            instrument: { type: 'spot', collection: 'INDEX', instrument_id: '^GSPC' },
            values: [0, -1, 0],
            clipped_mask: [false, false, false],
            price: { label: '^GSPC.close', values: [3200, 3250, 3275] },
          }],
          indicators: [{ input_id: 'X', indicator_id: 'exhaustion', series: [0, -1, 0] }],
          events: [{ input_id: 'X', block_id: 'b1', kind: 'long_entry', fired_indices: [1], latched_indices: [1] }],
          realized_pnl: [[0, 50, 75]],
          clipped: false,
          diagnostics: {},
        }),
      });
    });
  });

  test('down-cascade flow: pick Exhaustion, set eq -1, run', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
    const failedResponses = [];
    page.on('response', (r) => {
      if (r.status() >= 400 && r.url().includes('/api/')) {
        failedResponses.push(`${r.request().method()} ${r.status()} ${r.url()}`);
      }
    });

    await page.goto(`${BASE}/signals`);
    await expect(page.getByText('Exhaustion Test')).toBeVisible();

    // The seeded block + condition should already be present.
    await expect(page.getByTestId('block-0')).toBeVisible();
    await expect(page.getByTestId('condition-0-0')).toBeVisible();

    // --- Operand: LHS slot -> open menu -> Indicator -------------------------
    const lhsAdd = page.getByTestId('condition-0-0').getByTestId('operand-add-btn').first();
    await lhsAdd.click();
    await page.getByTestId('operand-menu-indicator').click();

    // The indicator <select> should now render and contain BOTH new defaults.
    const indSelect = page.getByTestId('operand-indicator-select');
    await expect(indSelect).toBeVisible();
    const optionValues = await indSelect.locator('option').evaluateAll(
      (opts) => opts.map((o) => o.value),
    );
    const optionLabels = await indSelect.locator('option').evaluateAll(
      (opts) => opts.map((o) => o.textContent.trim()),
    );
    // eslint-disable-next-line no-console
    console.log('OPERAND DROPDOWN values:', JSON.stringify(optionValues));
    // eslint-disable-next-line no-console
    console.log('OPERAND DROPDOWN labels:', JSON.stringify(optionLabels));

    await page.screenshot({ path: `${OUT}/repro-operand-dropdown.png`, fullPage: true });

    expect(optionValues, 'exhaustion missing from operand dropdown').toContain('exhaustion');
    expect(optionValues, 'nthtap missing from operand dropdown').toContain('nthtap');

    // Selecting exhaustion should install it + render its param panel.
    await indSelect.selectOption('exhaustion');
    await expect(indSelect).toHaveValue('exhaustion');
    // Bind the indicator's input to X so the operand is complete.
    await page.getByTestId('operand-indicator-input').selectOption('X');

    // --- Comparator -> eq ----------------------------------------------------
    await page.getByTestId('op-select-0-0').selectOption('eq');

    // --- RHS operand: Constant = -1 (the prime suspect) ----------------------
    const rhsAdd = page.getByTestId('condition-0-0').getByTestId('operand-add-btn').first();
    await rhsAdd.click();
    await page.getByTestId('operand-menu-constant').click();

    const constInput = page.getByTestId('operand-constant-input');
    await expect(constInput).toBeVisible();
    await constInput.fill('-1');
    // ASSERT the input actually holds -1 (not clamped, not stripped, not 0).
    await expect(constInput).toHaveValue('-1');
    // Negative value must survive a blur (onBlur reverts only on NaN).
    await constInput.blur();
    await expect(constInput).toHaveValue('-1');

    await page.screenshot({ path: `${OUT}/repro-configured-condition.png`, fullPage: true });

    // --- Run -----------------------------------------------------------------
    const runBtn = page.getByTestId('run-signal-btn');
    await expect(runBtn).toBeEnabled();
    await runBtn.click();
    await expect(page.getByTestId('results-plot-unified')).toBeVisible({ timeout: 8000 });

    expect(failedResponses, `failed API responses: ${failedResponses.join('\n')}`).toEqual([]);
    expect(consoleErrors, `console errors: ${consoleErrors.join('\n')}`).toEqual([]);
  });

  test('eq 1 (up-cascade) and ge N (nthtap count) also accepted', async ({ page }) => {
    await page.goto(`${BASE}/signals`);
    await expect(page.getByTestId('condition-0-0')).toBeVisible();

    // LHS -> NthTap
    await page.getByTestId('condition-0-0').getByTestId('operand-add-btn').first().click();
    await page.getByTestId('operand-menu-indicator').click();
    const indSelect = page.getByTestId('operand-indicator-select');
    await indSelect.selectOption('nthtap');
    await expect(indSelect).toHaveValue('nthtap');
    await page.getByTestId('operand-indicator-input').selectOption('X');

    // ge with a positive integer constant
    await page.getByTestId('op-select-0-0').selectOption('ge');
    await page.getByTestId('condition-0-0').getByTestId('operand-add-btn').first().click();
    await page.getByTestId('operand-menu-constant').click();
    const constInput = page.getByTestId('operand-constant-input');
    await constInput.fill('3');
    await expect(constInput).toHaveValue('3');

    // Switch comparator to eq and value to 1 (up-cascade idiom)
    await page.getByTestId('op-select-0-0').selectOption('eq');
    await constInput.fill('1');
    await expect(constInput).toHaveValue('1');
  });
});
