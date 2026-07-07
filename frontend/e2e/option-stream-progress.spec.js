// E2E regression for the user-reported "I don't see the progress percents"
// bug. The percentage was rendered but flex:1 on the chart's loading-state
// row let it grow to fill the panel, centring text below the viewport (at
// y=1621 in the failing instance, panel was ~1700px tall). Fixed by
// pinning the row to the top with intrinsic height + padding.

import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:5173';

test('option_stream Computing-state shows the percentage in the visible viewport', async ({ page }) => {
  await page.addInitScript(() => {
    try { window.localStorage.clear(); } catch { /* ignore */ }
  });

  await page.route('**/api/data/collections*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ collections: ['INDEX'] }),
  }));
  await page.route('**/api/data/INDEX*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ items: [{ symbol: 'IND_SP_500', asset_class: 'INDEX', collection: 'INDEX' }], total: 1, skip: 0, limit: 500 }),
  }));
  await page.route('**/api/options/roots*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      roots: [{ collection: 'OPT_SP_500', name: 'SP 500', has_greeks: true, providers: ['IVOLATILITY'], expiration_first: '2005-01-21', expiration_last: '2027-12-19', doc_count_estimated: 0, strike_factor_verified: true, last_trade_date: '2024-12-20' }],
    }),
  }));
  let computeStartedAt = 0;
  await page.route('**/api/indicators/compute', async (r) => {
    computeStartedAt = Date.now();
    await new Promise((res) => setTimeout(res, 4000));
    await r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ dates: [20240620], series: [{ label: 'atm_iv', collection: 'OPT_SP_500', instrument_id: 'stream', close: [0.18] }], values: [0.18] }),
    });
  });
  await page.route('**/api/indicators/progress/*', async (r) => {
    const elapsed = computeStartedAt ? Date.now() - computeStartedAt : 0;
    const frac = Math.min(0.9, elapsed / 4000);
    await r.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ done: Math.round(frac * 100), total: 100, fraction: frac }),
    });
  });

  await page.goto(`${BASE}/indicators`);
  await page.waitForLoadState('networkidle');

  const defaultHeader = page.getByTestId('category-default');
  if ((await defaultHeader.getAttribute('data-collapsed')) === 'true') {
    await defaultHeader.click();
  }

  const atm = page.getByText('ATM contract IV', { exact: true });
  await expect(atm).toBeVisible({ timeout: 10000 });
  await atm.locator('..').click({ force: true });

  const runBtn = page.getByRole('button', { name: /Run indicator/i });
  await expect(runBtn).toBeEnabled({ timeout: 5000 });
  await runBtn.click();

  await page.waitForTimeout(1500);

  const chartPanel = page.getByTestId('results-card');
  // Single combined element: "Computing... 37%". Bounding box must lie
  // within the visible viewport so the user actually sees it.
  const computingText = chartPanel.getByText(/Computing\.\.\. \d+%/);
  await expect(computingText).toBeVisible();
  const box = await computingText.boundingBox();
  const viewport = page.viewportSize();
  expect(box).toBeTruthy();
  expect(box.y).toBeLessThan(viewport.height);
  expect(box.y + box.height).toBeGreaterThan(0);
});

// ── Futures-notional sizing control on the option hold form (Wave 2 FE) ──────
// Drives the Signals inputs picker → Options drill-down → OptionStreamForm
// (showHoldControls) → enable hold → the shared "Sizing" control appears. Then
// verifies: defaults to Percentage (premium_notional); switching to Futures
// notional reveals the two-option Futures-reference dropdown (NO
// continuous_front) + the formula helper, and HIDES the premium-notional
// implied-leverage readout (Guardrail Sign 5). Reaches the SAME shared form the
// Portfolio page uses, so one spec covers both contexts.
test('futures-notional sizing control: mode toggle shows/hides the futures reference + hides the leverage readout', async ({ page }) => {
  await page.addInitScript(() => {
    try { window.localStorage.clear(); } catch { /* ignore */ }
  });

  await page.route('**/api/data/collections*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ collections: ['INDEX'] }),
  }));
  await page.route('**/api/data/INDEX*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ items: [{ symbol: 'IND_SP_500', asset_class: 'INDEX', collection: 'INDEX' }], total: 1, skip: 0, limit: 500 }),
  }));
  await page.route('**/api/options/roots*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      roots: [{
        collection: 'OPT_SP_500', name: 'SP 500', has_greeks: true,
        providers: ['IVOLATILITY'], expiration_first: '2005-01-21',
        expiration_last: '2027-12-19', doc_count_estimated: 0,
        strike_factor_verified: true, last_trade_date: '2024-12-20',
        cycles: ['M', 'W3 Friday'],
      }],
    }),
  }));
  // Premium-mode implied-leverage probe — return a resolvable contract so the
  // premium-mode readout has data (its presence in premium mode / absence in
  // futures mode is what the test asserts).
  await page.route('**/api/options/select*', (r) => r.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ contract: { strike: 5000 }, premium_mid: 25 }),
  }));
  // Persistence GETs — return empty so the Signals page isn't gated on the
  // backend at :8000 (unavailable in this session; signals are localStorage-backed).
  await page.route('**/api/persistence/**', (r) => r.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify([]),
  }));

  await page.goto(`${BASE}/signals`);
  await page.getByTestId('add-signal-btn').click();
  await page.getByTestId('inputs-add-btn').click();
  await page.getByTestId('input-picker-0').click();

  // Drill into the Options tab → the shared OptionStreamForm mounts with the
  // signals hold controls (showOptionHoldControls=true on InputsPanel).
  await page.getByTestId('picker-options-toggle').click();
  const form = page.getByTestId('option-stream-form');
  await expect(form).toBeVisible();
  await expect(form.locator('select[aria-label="Root"]')).not.toHaveValue('', { timeout: 15000 });

  // Enable "Hold contract between rolls" → the Sizing + Size% block appears.
  await page.getByTestId('hold-between-rolls').check();

  // Sizing mode defaults to Percentage (premium_notional); no futures reference.
  const sizing = page.getByTestId('sizing-mode');
  await expect(sizing).toBeVisible();
  await expect(sizing).toHaveValue('premium_notional');
  await expect(page.getByTestId('futures-reference')).toHaveCount(0);
  await expect(page.getByTestId('futures-notional-help')).toHaveCount(0);

  // Switch to Futures notional.
  await sizing.selectOption('futures_notional');

  // Futures-reference dropdown appears with EXACTLY two options and NO
  // continuous_front; the formula helper is shown; nav_times stays exposed.
  const ref = page.getByTestId('futures-reference');
  await expect(ref).toBeVisible();
  await expect(ref.locator('option')).toHaveCount(2);
  const refValues = await ref.locator('option').evaluateAll((os) => os.map((o) => o.value));
  expect(refValues).toEqual(['nearest_on_or_after', 'nearest_abs']);
  expect(refValues).not.toContain('continuous_front');
  await expect(page.getByTestId('futures-notional-help')).toBeVisible();
  await expect(page.getByTestId('nav-times')).toBeVisible();

  // Guardrail Sign 5: the premium-notional implied-leverage readout is hidden
  // in futures mode (neither the data group nor the fallback hint renders).
  await expect(page.getByTestId('lev-readout-group')).toHaveCount(0);
  await expect(page.getByTestId('nav-hint')).toHaveCount(0);
});
