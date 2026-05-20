import { test, expect, type ConsoleMessage } from '@playwright/test';

/**
 * Sanity probe ported from `frontend-react-archive` style. Verifies that:
 *   - the dev-harness root loads
 *   - the sidebar renders (TcgSidebarComponent is wired)
 *   - the Data page renders inside the router-outlet (redirect from `/`)
 *   - no `console.error` calls during initial load
 */
test.describe('quick-check', () => {
  test('dev-harness root loads with sidebar and no console errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg: ConsoleMessage) => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    });
    page.on('pageerror', (err) => {
      errors.push(String(err));
    });

    await page.addInitScript(() => {
      try { window.localStorage.clear(); } catch { /* ignore */ }
    });

    await page.goto('/');

    // Sidebar nav anchors are produced by TcgSidebarComponent's
    // routerLink-active loop. Each top-level item renders as an `<a>` with
    // routerLink set; we wait for at least one to be visible.
    await expect(page.locator('tcg-sidebar')).toBeVisible();
    await expect(page.locator('tcg-sidebar a').first()).toBeVisible();

    // The redirect from `/` goes to `/data`. Verify URL settled and the
    // data page component mounted.
    await expect(page).toHaveURL(/\/data\/?$/);
    await expect(page.locator('tcg-data-page')).toBeVisible();

    // Filter out benign warnings about the dev http2 (Angular CLI dev
    // server occasionally logs noisy fetch errors when the stub backend
    // is restarting). We treat any `console.error` as a regression.
    const significant = errors.filter((e) =>
      !/Failed to load resource|favicon\.ico|net::ERR_/.test(e)
    );
    expect(significant, `unexpected console errors: ${significant.join('\n')}`)
      .toEqual([]);
  });
});
