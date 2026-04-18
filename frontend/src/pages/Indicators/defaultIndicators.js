// Registry of read-only default indicators shipped with the UI.
//
// Each entry uses the typed-signature convention the backend now enforces:
//   def compute(series, <name>: <int|float|bool> = <literal>, ...)
// and accesses named series via ``series['label']`` so the frontend can
// auto-render one slot per label.
//
// Contract per entry:
//   - ``id``       stable string id; used as key in localStorage defaultState.
//   - ``name``     user-visible label (not editable).
//   - ``readonly`` always ``true`` — code + name locked; params + series
//                  picks are still user-editable per-session.
//   - ``code``     Python source string.
//
// IMPORTANT: the name and code here are the canonical source of truth.
// They are NEVER overwritten by localStorage contents — only per-session
// param / series picks persist (see ``defaultState`` in storage.js).

export const DEFAULT_INDICATORS = [
  {
    id: 'sma-20',
    name: '20-day SMA',
    readonly: true,
    code: `def compute(series, window: int = 20):
    s = series['price']
    out = np.full_like(s, np.nan, dtype=float)
    out[window-1:] = np.convolve(s, np.ones(window)/window, mode='valid')
    return out`,
  },
];
