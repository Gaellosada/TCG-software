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
