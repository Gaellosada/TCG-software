// API base + backend origin, in both run modes:
//  - normal browser (dev: Vite proxy; web prod: same-origin) -> relative '/api'
//  - Tauri desktop webview (origin tauri://localhost or, on Windows,
//    http://tauri.localhost — no Vite proxy) -> the sidecar's absolute URL.
//
// SIDECAR_ORIGIN is the SINGLE SOURCE OF TRUTH for the backend host:port: the
// Rust wrapper (src-tauri/src/lib.rs SIDECAR_HOST/PORT) and the sidecar's
// --port must match it, and HEALTH_URL/API_BASE both derive from it.
const SIDECAR_ORIGIN = 'http://127.0.0.1:8000';

// True only inside the Tauri desktop webview. Tauri v2 injects
// window.__TAURI_INTERNALS__ before app JS runs, so this is reliable at module
// load and any time after. Pure (takes the window) so it is unit-testable for
// both environments without touching the real global. Used to gate desktop-only
// UI; in a normal browser it is false and web mode is byte-for-byte unchanged.
export function isTauri(w = (typeof window !== 'undefined' ? window : undefined)) {
  return Boolean(w && w.__TAURI_INTERNALS__);
}

// Resolve the API base for a given window. Exposed as a pure helper so the
// branch is unit-testable without import-time global mutation.
export function resolveApiBase(w = (typeof window !== 'undefined' ? window : undefined)) {
  return isTauri(w) ? `${SIDECAR_ORIGIN}/api` : '/api';
}

// Backend health endpoint (root, NOT under /api). Single source of truth so the
// Settings reconnect poll and the backend-down banner don't each hardcode the
// host:port (a port change would otherwise mean editing three files).
export const HEALTH_URL = `${SIDECAR_ORIGIN}/health`;

// Resolved once at module load: Tauri injects __TAURI_INTERNALS__ before app JS
// runs, so eager resolution is correct for the packaged webview, and callers
// can use API_BASE as a plain string.
export const API_BASE = resolveApiBase();

// Defensive: if we are clearly inside a Tauri webview (document served from the
// tauri scheme / tauri.localhost host on Windows) yet API_BASE resolved to the
// browser '/api', every backend call would silently 404. That should be
// impossible (the global is injected first), but warn loudly rather than fail
// silently so the cause is obvious instead of a mystery data outage.
{
  const loc = typeof window !== 'undefined' ? window.location : undefined;
  const looksLikeTauriDoc =
    !!loc &&
    ((typeof loc.protocol === 'string' && loc.protocol.startsWith('tauri')) ||
      (typeof loc.hostname === 'string' && loc.hostname === 'tauri.localhost'));
  if (API_BASE === '/api' && looksLikeTauriDoc) {
    // eslint-disable-next-line no-console
    console.warn(
      '[tcg] API_BASE resolved to "/api" inside a Tauri webview — backend calls ' +
        'will 404. window.__TAURI_INTERNALS__ was not present at module load.',
    );
  }
}
