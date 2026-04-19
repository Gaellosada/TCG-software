import { test, expect } from '@playwright/test';

// End-to-end spec for the default indicator library (Wave 1).
// Exercises four representative defaults end-to-end: SMA, RSI, MACD Line,
// Bollinger %B. Each test: opens the Indicators page, picks the indicator
// from the left list, waits for the series slot to be auto-filled (the
// page resolves INDEX/^GSPC on mount), clicks Run, and asserts the Plotly
// chart renders with no error card.
//
// All network calls are mocked — no live backend required. The mock setup
// mirrors the sibling indicators.spec.js file so both specs can run
// independently or together.

const BASE = 'http://localhost:5173';

// Install route mocks that the Indicators page needs on every test.
async function installMocks(page) {
  // Clear any persisted state from a prior run.
  await page.addInitScript(() => {
    try { window.localStorage.clear(); } catch { /* ignore */ }
  });

  // Collections discovery — needed by resolveDefaultIndexInstrument.
  await page.route('**/api/data/collections*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ collections: ['INDEX', 'VOL'] }),
    });
  });

  // INDEX instruments — ^GSPC matches isSnpSymbol → auto-fills the close slot.
  await page.route('**/api/data/INDEX*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          { symbol: '^GSPC', asset_class: 'INDEX', collection: 'INDEX' },
          { symbol: 'NDX',   asset_class: 'INDEX', collection: 'INDEX' },
        ],
        total: 2,
        skip: 0,
        limit: 500,
      }),
    });
  });

  await page.route('**/api/data/VOL*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [{ symbol: '^VIX', asset_class: 'INDEX', collection: 'VOL' }],
        total: 1,
        skip: 0,
        limit: 500,
      }),
    });
  });

  // Compute endpoint — echo the series labels back with a trivial indicator
  // array so IndicatorChart's hasData branch is reached.
  await page.route('**/api/indicators/compute', async (route) => {
    const postData = route.request().postDataJSON() || {};
    const labels = Object.keys(postData.series || {});
    const series = labels.map((label) => {
      const ref = postData.series[label];
      return {
        label,
        collection: ref.collection,
        instrument_id: ref.instrument_id,
        close: [100.0, 101.0, 102.0],
      };
    });
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        dates: ['2024-01-01', '2024-01-02', '2024-01-03'],
        series,
        indicator: [null, null, 101.0],
      }),
    });
  });

  // seriesSummary is fetched lazily (details panel) — mock to avoid
  // hanging requests from any expanded rows.
  await page.route('**/api/data/series-summary*', async (route) => {
    await route.fulfill({ status: 404, body: '{}' });
  });
}

// Navigate to the Indicators page and wait until the default list loads.
async function gotoIndicators(page) {
  await page.goto(`${BASE}/indicators`);
  // Wait for the DEFAULT section header which confirms the list has rendered.
  await page.waitForSelector('[data-testid="category-default"]', { timeout: 10000 });
}

// Select an indicator by its visible name in the left panel, wait for the
// series slot to be auto-filled, then click Run and assert the chart renders.
//
// Series slots for default indicators are auto-filled via
// resolveDefaultIndexInstrument (which uses the mocked collections +
// INDEX endpoints). We wait for the Run button to become enabled, which
// confirms the slot is populated, then click it.
async function runAndAssertChart(page, indicatorName) {
  // Click the indicator row in the list.
  // IndicatorsList renders each item as a span.rowName inside a role=button div.
  // We use visible text matching; only exact indicator names are in the list so
  // this is unambiguous (no partial matches needed given the name set).
  await page.locator(`[data-category="default"] >> text="${indicatorName}"`).first().click();

  // Wait for the Run button to become enabled.
  // canRun requires: indicator selected + all series slots filled + code present.
  // The SPX auto-fill happens asynchronously after mount — 8 s is generous.
  const runBtn = page.getByRole('button', { name: /Run indicator/i });
  await expect(runBtn).toBeEnabled({ timeout: 8000 });

  // Click Run.
  await runBtn.click();

  // Assert the Plotly chart renders — react-plotly.js mounts a .js-plotly-plot
  // div once there is data. Timeout of 8 s covers the mocked fetch latency.
  await expect(page.locator('.js-plotly-plot')).toBeVisible({ timeout: 8000 });

  // Assert no error card is shown.
  await expect(page.locator('[data-error-type]')).toHaveCount(0);
}

test.describe('Default indicator library — e2e', () => {
  test('SMA renders chart on Run', async ({ page }) => {
    await installMocks(page);
    await gotoIndicators(page);
    await runAndAssertChart(page, 'SMA');
  });

  test('RSI renders chart on Run', async ({ page }) => {
    await installMocks(page);
    await gotoIndicators(page);
    await runAndAssertChart(page, 'RSI');
  });

  test('MACD Line renders chart on Run', async ({ page }) => {
    await installMocks(page);
    await gotoIndicators(page);
    await runAndAssertChart(page, 'MACD Line');
  });

  test('Bollinger %B renders chart on Run', async ({ page }) => {
    await installMocks(page);
    await gotoIndicators(page);
    await runAndAssertChart(page, 'Bollinger %B');
  });
});
