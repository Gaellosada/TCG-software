import { test, expect } from '@playwright/test';

test.describe('Continuous Rolling - Data Section', () => {
  test('Help page documents continuous rolling', async ({ page }) => {
    await page.goto('/help');
    await page.waitForLoadState('networkidle');

    const rollingHeading = page.locator('h2:has-text("Continuous Futures Rolling")');
    await expect(rollingHeading).toBeVisible({ timeout: 10000 });

    await expect(page.locator('h3:has-text("Proportional")')).toBeVisible();
    await expect(page.locator('h3:has-text("Difference")')).toBeVisible();
    await expect(page.locator('h3:has-text("None")')).toBeVisible();
    await expect(page.locator('h3:has-text("Roll Dates")')).toBeVisible();

    await page.screenshot({
      path: 'e2e/screenshots/help-rolling-section.png',
      fullPage: true,
    });
  });

  test('Data page loads with Futures category and collections', async ({ page }) => {
    await page.goto('/data');
    await page.waitForLoadState('networkidle');

    // Expand Futures category
    const futuresHeader = page.locator('button:has-text("Futures")');
    await expect(futuresHeader).toBeVisible({ timeout: 15000 });
    await futuresHeader.click();

    // Should show FUT_VIX collection
    const futVix = page.locator('text=FUT_VIX');
    await expect(futVix).toBeVisible({ timeout: 10000 });

    // Click FUT_VIX to expand its group
    await futVix.click();

    // Now "Continuous Series" should be visible
    const continuousEntry = page.locator('text=Continuous Series').first();
    await expect(continuousEntry).toBeVisible({ timeout: 5000 });

    await page.screenshot({
      path: 'e2e/screenshots/data-futures-expanded.png',
      fullPage: true,
    });
  });

  test('Continuous series chart renders with controls and roll dates', async ({ page }) => {
    await page.goto('/data');
    await page.waitForLoadState('networkidle');

    // Navigate to FUT_VIX continuous series
    const futuresHeader = page.locator('button:has-text("Futures")');
    await expect(futuresHeader).toBeVisible({ timeout: 15000 });
    await futuresHeader.click();

    const futVix = page.locator('text=FUT_VIX');
    await expect(futVix).toBeVisible({ timeout: 10000 });
    await futVix.click();

    const continuousEntry = page.locator('text=Continuous Series').first();
    await expect(continuousEntry).toBeVisible({ timeout: 5000 });
    await continuousEntry.click();

    // Wait for chart to load (API call + render)
    await page.waitForTimeout(5000);

    // Verify controls exist
    const adjustmentLabel = page.locator('text=Adjustment');
    await expect(adjustmentLabel).toBeVisible({ timeout: 10000 });

    // Verify chart renders (Plotly creates .js-plotly-plot)
    const plotlyChart = page.locator('.js-plotly-plot');
    await expect(plotlyChart).toBeVisible({ timeout: 15000 });

    // Verify metadata shows
    const barsInfo = page.locator('text=/\\d+ bars/');
    await expect(barsInfo).toBeVisible({ timeout: 5000 });

    // Verify roll count is shown
    const rollInfo = page.locator('text=/\\d+ rolls?/');
    await expect(rollInfo).toBeVisible({ timeout: 5000 });

    // Verify contract count is shown
    const contractInfo = page.locator('text=/\\d+ contracts?/');
    await expect(contractInfo).toBeVisible({ timeout: 5000 });

    await page.screenshot({
      path: 'e2e/screenshots/continuous-series-none.png',
      fullPage: true,
    });

    // Switch to proportional adjustment
    const adjustmentSelect = page.locator('select').first();
    await adjustmentSelect.selectOption('proportional');
    await page.waitForTimeout(5000);

    await page.screenshot({
      path: 'e2e/screenshots/continuous-series-proportional.png',
      fullPage: true,
    });
  });
});
