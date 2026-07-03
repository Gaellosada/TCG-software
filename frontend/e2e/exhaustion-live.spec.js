import { test, expect } from '@playwright/test';

// LIVE end-to-end: drives the real Signals UI against the REAL backend
// (./start.sh on :8000 + Vite :5173). The ONLY mocked call is the signals
// LIST endpoint (used to inject a runnable seed signal so the test doesn't
// have to drive instrument selection through the data UI); the actual
// `/api/signals/compute` is NOT mocked and hits the live backend + dwh data.
//
// The seed references the shipped `exhaustion` default via an IndicatorOperand
// inside a Compare(eq -1) condition on a real S&P 500 instrument (ETF_SPY)
// with upper=440, lower=432, window=15, ma_window=20 — the same setup the
// numerical proof confirmed fires 3x.
const BASE = process.env.TCG_E2E_BASE || 'http://localhost:5173';
const OUT = '/home/gael/claude_workspace/trajectoire_cap/workspace/tasks/indicator-stateful-helpers/output';

const SPOT = { type: 'spot', collection: 'ETF', instrument_id: 'ETF_SPY' };
const PARAMS = { upper: 440.0, lower: 432.0, window: 15, ma_window: 20 };

// Shipped exhaustion compute code — sent verbatim in the live compute call.
// Kept in sync with frontend/src/pages/Indicators/defaults/exhaustion.js.
const EXH_CODE = `def compute(series, upper: float = 70.0, lower: float = 30.0, window: int = 10, ma_window: int = 20):
    assert upper > lower, 'upper must be strictly greater than lower'
    assert window >= 1, 'window must be >= 1'
    assert ma_window >= 1, 'ma_window must be >= 1'
    s = series['close']
    n = s.shape[0]
    ma = np.full(n, np.nan, dtype=float)
    if n >= ma_window:
        ma[ma_window - 1:] = np.convolve(s, np.ones(ma_window) / ma_window, mode='valid')
    down = ta.sequence_within(
        [ta.crossed_down(ma, upper), ta.crossed_down(ma, lower)],
        window,
        abort=ta.crossed_up(ma, upper),
    )
    up = ta.sequence_within(
        [ta.crossed_up(ma, lower), ta.crossed_up(ma, upper)],
        window,
        abort=ta.crossed_down(ma, lower),
    )
    out = np.where(
        np.isnan(down) | np.isnan(up),
        np.nan,
        (up == 1.0).astype(float) - (down == 1.0).astype(float),
    )
    return out`;

const SEED_SIGNAL_DOC = {
  id: 'sig-exh-live',
  name: 'Exhaustion LIVE',
  category: 'RESEARCH',
  locked: false,
  description: '',
  inputs: [{ id: 'X', instrument: SPOT }],
  rules: {
    entries: [{
      id: 'b1',
      name: 'Block 1',
      input_id: 'X',
      weight: 1,
      enabled: true,
      conditions: [{
        op: 'eq',
        lhs: {
          kind: 'indicator',
          indicator_id: 'exhaustion',
          input_id: 'X',
          output: 'default',
          params_override: PARAMS,
          series_override: { close: 'X' },
        },
        rhs: { kind: 'constant', value: -1 },
      }],
      description: '',
    }],
    // An exit so the entry-only Run-gate is satisfied (positions can close).
    exits: [{
      id: 'x1',
      name: 'Exit 1',
      target_entry_block_names: ['Block 1'],
      enabled: true,
      conditions: [{
        op: 'eq',
        lhs: {
          kind: 'indicator',
          indicator_id: 'exhaustion',
          input_id: 'X',
          output: 'default',
          params_override: PARAMS,
          series_override: { close: 'X' },
        },
        rhs: { kind: 'constant', value: 1 },
      }],
      description: '',
    }],
    resets: [],
  },
  settings: {},
};

test.describe('Exhaustion LIVE end-to-end (real backend)', () => {
  test('Compare(Exhaustion eq -1) on ETF_SPY fires via the live compute', async ({ page }) => {
    // Mock ONLY the signals LIST so the seed loads; everything else is live.
    await page.route('**/api/persistence/signals?*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([SEED_SIGNAL_DOC]),
      });
    });
    // Swallow autosave writes (we don't want to mutate live app-data).
    await page.route('**/api/persistence/signals/**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SEED_SIGNAL_DOC) });
    });

    // Capture the LIVE compute response to prove it hit the real backend.
    let computeStatus = null;
    let computeBody = null;
    page.on('response', async (r) => {
      if (r.url().includes('/api/signals/compute')) {
        computeStatus = r.status();
        try { computeBody = await r.json(); } catch { /* ignore */ }
      }
    });

    await page.goto(`${BASE}/signals`);
    await expect(page.getByText('Exhaustion LIVE')).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId('condition-0-0')).toBeVisible();

    // The condition must show Exhaustion / == / -1 already configured.
    await expect(page.getByTestId('operand-indicator-select').first()).toHaveValue('exhaustion');

    const runBtn = page.getByTestId('run-signal-btn');
    await expect(runBtn).toBeEnabled({ timeout: 10000 });
    await runBtn.click();

    // Live compute renders the unified results plot.
    await expect(page.getByTestId('results-plot-unified')).toBeVisible({ timeout: 30000 });

    await page.screenshot({ path: `${OUT}/live-exhaustion-result.png`, fullPage: true });

    // Prove the call hit the live backend and the signal fired.
    expect(computeStatus, 'live /api/signals/compute did not return 200').toBe(200);
    expect(computeBody).toBeTruthy();
    const events = (computeBody.events || []).filter((e) => (e.fired_indices || []).length > 0);
    const totalFires = events.reduce((acc, e) => acc + (e.fired_indices || []).length, 0);
    // eslint-disable-next-line no-console
    console.log('LIVE compute: status', computeStatus, 'fires', totalFires,
      'events', JSON.stringify(events.map((e) => ({ block: e.block_id, kind: e.kind, fired: e.fired_indices }))));
    expect(totalFires, 'live signal did not fire any entries').toBeGreaterThan(0);
  });
});
