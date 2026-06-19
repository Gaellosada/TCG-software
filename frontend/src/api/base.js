// API base: absolute backend URL inside the Tauri desktop webview (origin
// tauri://localhost has no Vite proxy), relative '/api' in a normal browser
// (dev: Vite proxy; web prod: same-origin). Tauri v2 always injects
// window.__TAURI_INTERNALS__ before app JS runs, so this resolves correctly
// at module load. Port 8000 matches the sidecar (src-tauri capability/lib.rs).
//
// Exposed as a pure helper (resolveApiBase) so the branch is unit-testable
// without import-time global mutation; API_BASE is the resolved value used by
// all callers.
export function resolveApiBase(w = (typeof window !== 'undefined' ? window : undefined)) {
  return w && w.__TAURI_INTERNALS__ ? 'http://127.0.0.1:8000/api' : '/api';
}

export const API_BASE = resolveApiBase();

// True only inside the Tauri desktop webview. Tauri v2 injects
// window.__TAURI_INTERNALS__ before app JS runs, so this is reliable at any
// time after load. Pure (takes the window) so it is unit-testable for both
// environments without touching the real global. Used to gate desktop-only UI
// (the DB-credentials Settings section and the backend-down banner); in a
// normal browser it is false and web mode is byte-for-byte unchanged.
export function isTauri(w = (typeof window !== 'undefined' ? window : undefined)) {
  return Boolean(w && w.__TAURI_INTERNALS__);
}
