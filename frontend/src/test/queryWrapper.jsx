/**
 * Test helpers for rendering components that use TanStack Query.
 *
 * NOTE: ``src/test/setup.js`` already auto-wraps every RTL ``render`` in a
 * fresh QueryClientProvider, so a *bare* ``render(<Comp/>)`` works without any
 * helper. Use the helpers here only when a test needs CONTROL over the client
 * — specifically to share ONE client across multiple renders (e.g. to prove an
 * unmount→remount hits a warm cache, the no-spinner-on-navigation behaviour).
 *
 * ``renderWithClient`` passes its client through RTL's ``wrapper`` option. The
 * global auto-wrap composes a test-supplied ``wrapper`` INSIDE its own
 * provider, so the client passed here becomes the effective (innermost) one
 * that the component's hooks read.
 */
import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

/** Fresh client with retries off and an effectively-infinite gcTime. */
export function makeTestClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        // Keep cached data for the whole test so a remount in the same test
        // reliably reads the warm cache (default gcTime could evict between
        // an unmount and the next mount within a fake-timer window).
        gcTime: Infinity,
      },
    },
  });
}

/**
 * Render ``ui`` with a controllable QueryClient.
 * @param {React.ReactElement} ui
 * @param {{ client?: import('@tanstack/react-query').QueryClient }} [options]
 * @returns RTL render result, plus the ``client`` used (so tests can assert on
 *          cache state or reuse it for a second render).
 */
export function renderWithClient(ui, { client, ...options } = {}) {
  const queryClient = client ?? makeTestClient();
  function Wrapper({ children }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  const result = render(ui, { ...options, wrapper: Wrapper });
  return {
    ...result,
    client: queryClient,
    rerender: (rerenderUi) => result.rerender(rerenderUi),
  };
}
