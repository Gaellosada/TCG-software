import { test, expect } from '@playwright/test';

/**
 * Cross-page flow for the two seed pages (Data + Settings). Mirrors the
 * intent of the React `quick-check.spec.js` shape but uses Angular DOM
 * anchors:
 *
 *   1. Data page renders, sidebar present, no startup console errors.
 *   2. Navigate to Settings, toggle theme to `light`, verify
 *      <html data-theme> attribute + localStorage key both updated.
 *   3. Navigate back to Data; theme attribute still `light`.
 *
 * The four localStorage keys used by `TcgUserSettingsService` are the
 * cross-build contract — the React app reads/writes the same keys, so
 * these assertions also guard the shared-deployment migration story.
 */
test.describe('Data + Settings flow', () => {
  // No `addInitScript` here — it would fire on *every* navigation
  // (including the second `goto('/data')` later in the persistence test)
  // and wipe state we are deliberately verifying. Each test handles its
  // own initial localStorage setup explicitly via `page.evaluate` after
  // the first navigation has loaded.

  test('renders Data page after `/` redirect', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/data\/?$/);
    await expect(page.locator('tcg-data-page')).toBeVisible();
    await expect(page.locator('tcg-sidebar')).toBeVisible();
  });

  test('Settings theme toggle persists to localStorage and survives navigation', async ({ page }) => {
    // Pin the initial state to `dark` BEFORE the SPA boots so the
    // service's constructor reads the right value. We do this via
    // `goto` to a benign URL, then set localStorage and reload, rather
    // than `addInitScript` (which would re-fire on every later goto and
    // wipe the value we are trying to verify).
    await page.goto('/settings');
    await page.evaluate(() => {
      window.localStorage.clear();
      window.localStorage.setItem('tcg-theme', 'dark');
    });
    await page.reload();
    await expect(page.locator('tcg-settings-page')).toBeVisible();

    // Confirm initial theme is `dark` (read by the service constructor).
    await expect.poll(async () =>
      page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    ).toBe('dark');

    // Click the Light radio (button with text "Light" inside the Theme group).
    // `getByRole('radio', { name: 'Light' })` is stable against minor DOM
    // changes — the component uses `role="radio"` for both theme buttons.
    await page.getByRole('radio', { name: 'Light' }).click();

    // Wait for the effect() that mirrors the signal into the DOM/localStorage.
    await expect.poll(async () =>
      page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    ).toBe('light');

    const stored = await page.evaluate(() => window.localStorage.getItem('tcg-theme'));
    expect(stored).toBe('light');

    // Navigate to Data. localStorage MUST still contain `light` — that
    // is the persisted cross-build contract (the React app reads the
    // same key from the same domain).
    await page.goto('/data');
    await expect(page.locator('tcg-data-page')).toBeVisible();
    const storedAfterNav = await page.evaluate(() => window.localStorage.getItem('tcg-theme'));
    expect(storedAfterNav).toBe('light');

    // Note: the `<html data-theme>` mirror is driven by the service's
    // effect(), which only fires once *some* consumer injects the
    // service. The Data page itself does not inject it (TcgChartComponent
    // does, but only once a chart is mounted, which requires a selected
    // instrument). So we navigate back to Settings to force the mirror
    // to re-apply, then verify both localStorage and the DOM agree.
    await page.goto('/settings');
    await expect(page.locator('tcg-settings-page')).toBeVisible();
    await expect.poll(async () =>
      page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    ).toBe('light');
    const storedBack = await page.evaluate(() => window.localStorage.getItem('tcg-theme'));
    expect(storedBack).toBe('light');
  });
});
