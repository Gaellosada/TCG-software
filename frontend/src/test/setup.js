/**
 * Global test setup.
 *
 * Many components now read market/persistence data through TanStack Query, so
 * they require a ``QueryClientProvider`` in the tree. Rather than edit every
 * existing test to add a provider, we transparently wrap RTL's ``render`` so
 * EVERY rendered tree gets a fresh, isolated QueryClient by default.
 *
 * - A NEW client per ``render`` call → no cache leakage between tests.
 * - ``retry: false`` → a thrown api error surfaces immediately (tests assert
 *   error states without waiting out retry/backoff).
 * - If a test supplies its own ``wrapper``, we compose it INSIDE the provider
 *   so existing wrappers (routers, contexts) keep working.
 *
 * Tests that need to share one client across multiple renders (e.g. to prove
 * a warm-cache remount) should import ``renderWithClient`` from
 * ``src/test/queryWrapper`` and pass an explicit ``client`` — that bypasses
 * this auto-wrap by providing its own provider.
 */
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

// Always unmount between tests so a remount in the next test starts clean.
afterEach(() => {
  cleanup();
});

vi.mock('@testing-library/react', async (importOriginal) => {
  const actual = await importOriginal();
  const React = await import('react');
  const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query');

  // Compose a fresh-client provider with any test-supplied wrapper (the
  // test's wrapper renders INSIDE, so a test-controlled client there wins).
  function withProvider(options = {}) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    const InnerWrapper = options.wrapper;
    function Wrapper({ children }) {
      const inner = InnerWrapper
        ? React.createElement(InnerWrapper, null, children)
        : children;
      return React.createElement(QueryClientProvider, { client }, inner);
    }
    return { ...options, wrapper: Wrapper };
  }

  return {
    ...actual,
    render: (ui, options = {}) => actual.render(ui, withProvider(options)),
    renderHook: (cb, options = {}) => actual.renderHook(cb, withProvider(options)),
  };
});
