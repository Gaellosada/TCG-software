// @vitest-environment jsdom
import { describe, it, expect, afterEach, vi } from 'vitest';
import { classifyFetchError, FetchError } from './fetchError';

// Utility: toggle navigator.onLine for a single test.
function setOnline(on) {
  Object.defineProperty(window.navigator, 'onLine', {
    configurable: true,
    get: () => on,
  });
}

afterEach(() => {
  // Reset to default online.
  setOnline(true);
  vi.restoreAllMocks();
});

describe('classifyFetchError', () => {
  it('returns offline when navigator.onLine is false', () => {
    setOnline(false);
    const r = classifyFetchError(new TypeError('fetch failed'));
    expect(r.kind).toBe('offline');
    expect(r.title).toMatch(/offline/i);
  });

  it('classifies a raw TypeError as network', () => {
    setOnline(true);
    const err = new TypeError('Failed to fetch');
    const r = classifyFetchError(err);
    expect(r.kind).toBe('network');
    expect(r.message).toBe('Failed to fetch');
  });

  it('classifies a 404 Response as not-found', () => {
    setOnline(true);
    const res = { status: 404, ok: false };
    const r = classifyFetchError(null, res, 'No such thing');
    expect(r.kind).toBe('not-found');
    expect(r.message).toBe('No such thing');
    expect(r.status).toBe(404);
  });

  it('classifies a 500 Response as server', () => {
    setOnline(true);
    const res = { status: 503, ok: false };
    const r = classifyFetchError(null, res, 'boom');
    expect(r.kind).toBe('server');
    expect(r.message).toMatch(/503/);
  });

  it('classifies a 418 Response as client', () => {
    setOnline(true);
    const res = { status: 418, ok: false };
    const r = classifyFetchError(null, res);
    expect(r.kind).toBe('client');
    expect(r.status).toBe(418);
  });

  it('falls through to unknown on a non-TypeError generic throw', () => {
    setOnline(true);
    const r = classifyFetchError(new Error('weird thing'));
    expect(r.kind).toBe('unknown');
    expect(r.message).toBe('weird thing');
  });

  it('returns unknown when neither err nor res supplied', () => {
    setOnline(true);
    const r = classifyFetchError();
    expect(r.kind).toBe('unknown');
  });

  it('maps status 0 (opaque / blocked) to network, not unknown', () => {
    setOnline(true);
    const res = { status: 0, ok: false };
    const r = classifyFetchError(null, res);
    expect(r.kind).toBe('network');
    expect(r.status).toBe(0);
  });

  it('treats an AbortError as the neutral "aborted" kind', () => {
    setOnline(true);
    const e = new Error('The user aborted a request.');
    e.name = 'AbortError';
    const r = classifyFetchError(e);
    expect(r.kind).toBe('aborted');
  });

  it('aborted classification wins even when offline (user cancelled)', () => {
    setOnline(false);
    const e = Object.assign(new Error('aborted'), { name: 'AbortError' });
    const r = classifyFetchError(e);
    expect(r.kind).toBe('aborted');
  });

  it('wraps a FetchError that still carries kind/title/message', () => {
    const e = new FetchError({
      kind: 'network',
      title: 't',
      message: 'm',
      status: 0,
    });
    expect(e).toBeInstanceOf(Error);
    expect(e.kind).toBe('network');
    expect(e.title).toBe('t');
  });
});
