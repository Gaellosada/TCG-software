// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import BackendBanner from './BackendBanner';

afterEach(() => {
  cleanup();
  delete window.__TAURI_INTERNALS__;
  vi.restoreAllMocks();
});

function renderBanner() {
  return render(
    <MemoryRouter>
      <BackendBanner />
    </MemoryRouter>,
  );
}

describe('<BackendBanner>', () => {
  it('renders nothing in web mode (no Tauri global), regardless of backend state', () => {
    // The whole web build must be byte-for-byte unchanged: the banner is a
    // no-op without __TAURI_INTERNALS__. fetch is never even called.
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    renderBanner();
    expect(screen.queryByTestId('backend-banner')).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('under Tauri, shows the banner when /health is unreachable', async () => {
    window.__TAURI_INTERNALS__ = {};
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('refused'));

    renderBanner();

    await waitFor(() => {
      expect(screen.getByTestId('backend-banner')).toBeDefined();
    });
    // Links to the Settings route.
    const link = screen.getByRole('link', { name: /settings/i });
    expect(link.getAttribute('href')).toBe('/settings');
  });

  it('under Tauri, stays hidden when /health is OK', async () => {
    window.__TAURI_INTERNALS__ = {};
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true });

    renderBanner();

    // Give the first poll a chance to resolve, then assert still hidden.
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(screen.queryByTestId('backend-banner')).toBeNull();
  });
});
