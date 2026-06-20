import { describe, it, expect } from 'vitest';

import { resolveApiBase, API_BASE, isTauri, HEALTH_URL } from './base';

// API_BASE is computed once at import time, so we test the pure resolver
// (resolveApiBase) for both environments and assert the import-time default
// matches the browser/test environment (jsdom has no __TAURI_INTERNALS__).
describe('resolveApiBase', () => {
  it('returns relative /api in a normal browser (no Tauri global)', () => {
    // No __TAURI_INTERNALS__ → dev uses the Vite proxy, web prod is
    // same-origin. The web path MUST stay byte-for-byte '/api' so existing
    // fetch mocks that match '/api/...' keep matching.
    expect(resolveApiBase({})).toBe('/api');
  });

  it('returns the absolute sidecar URL inside the Tauri webview', () => {
    // Tauri v2 injects window.__TAURI_INTERNALS__ before app JS runs; the
    // tauri://localhost origin has no Vite proxy, so calls must target the
    // 127.0.0.1:8000 sidecar directly (port matches src-tauri/lib.rs).
    expect(resolveApiBase({ __TAURI_INTERNALS__: {} })).toBe('http://127.0.0.1:8000/api');
  });

  it('treats a missing window as a browser (relative /api)', () => {
    // Defensive: SSR/no-window environments must not accidentally pick the
    // Tauri branch.
    expect(resolveApiBase(undefined)).toBe('/api');
  });

  it('API_BASE resolves to /api in the jsdom test environment', () => {
    // Guards the regression this whole change protects: under tests there is
    // no Tauri global, so the resolved constant stays relative and all the
    // existing '/api/...' fetch mocks remain valid.
    expect(API_BASE).toBe('/api');
  });
});

describe('isTauri', () => {
  it('is false in a normal browser (no __TAURI_INTERNALS__)', () => {
    // Gates the desktop-only creds UI + banner off in the web build.
    expect(isTauri({})).toBe(false);
  });

  it('is true inside the Tauri webview', () => {
    expect(isTauri({ __TAURI_INTERNALS__: {} })).toBe(true);
  });

  it('treats a missing window as not-Tauri', () => {
    expect(isTauri(undefined)).toBe(false);
  });

  it('returns false in the jsdom test environment (no Tauri global)', () => {
    // The whole point: tests run as web mode, so the creds section never mounts.
    expect(isTauri()).toBe(false);
  });
});

describe('HEALTH_URL', () => {
  it('points at the sidecar /health (root, NOT under /api)', () => {
    expect(HEALTH_URL).toBe('http://127.0.0.1:8000/health');
  });

  it('shares the sidecar host:port with the Tauri API base (single source of truth)', () => {
    // Both derive from SIDECAR_ORIGIN, so a port change moves them together —
    // this is the drift the dedup (Settings + banner no longer hardcode it)
    // prevents.
    const apiOrigin = resolveApiBase({ __TAURI_INTERNALS__: {} }).replace(/\/api$/, '');
    expect(HEALTH_URL.startsWith(apiOrigin)).toBe(true);
  });
});
