import styles from './IntradayBacktestPage.module.css';

// ---------------------------------------------------------------------------
// ConditionEditor — one row of a Conditions builder.
//
// Renders the param inputs for a single condition (its `type` decides which
// inputs) plus a remove ×. The condition types are PINNED in DESIGN.md and
// hardcoded here — the frontend does NOT fetch them. The SAME editor serves two
// contexts, each with its OWN allowed-types set (the "Add condition" dropdown in
// the host module is fed the matching catalogue, so contexts never cross):
//
//  ENTRY/EXIT (v2 — CONDITION_TYPES):
//   - max_spread          { pct, min_ticks }
//   - min_quote_size      { size }
//   - min_premium         { points }
//   - max_underlying_move { pct, ref }
//
//  HEDGE (v4 — HEDGE_CONDITION_TYPES):
//   - max_spread          { pct, min_ticks }
//   - min_quote_size      { size }
//   - min_rehedge_delta   { threshold }
// ---------------------------------------------------------------------------

// Entry/exit dropdown catalogue: order + label shown in the "Add condition"
// select for the ENTRY and EXIT modules.
export const CONDITION_TYPES = [
  { type: 'max_spread', label: 'Max spread' },
  { type: 'min_quote_size', label: 'Min quote size' },
  { type: 'min_premium', label: 'Min premium' },
  { type: 'max_underlying_move', label: 'Max underlying move' },
];

// Hedge dropdown catalogue (v4): the three condition types the HEDGE module
// offers. max_spread / min_quote_size are shared with entry/exit;
// min_rehedge_delta is hedge-only. Kept SEPARATE from CONDITION_TYPES so the
// two contexts never cross-contaminate their "Add condition" menus.
export const HEDGE_CONDITION_TYPES = [
  { type: 'max_spread', label: 'Max spread' },
  { type: 'min_quote_size', label: 'Min quote size' },
  { type: 'min_rehedge_delta', label: 'Min rehedge delta' },
];

// Label lookup spans BOTH catalogues (duplicate keys resolve to the same label)
// so `conditionLabel` works regardless of the row's originating context.
const LABELS = [...CONDITION_TYPES, ...HEDGE_CONDITION_TYPES].reduce((acc, t) => {
  acc[t.type] = t.label;
  return acc;
}, {});

// Monotonic id so freshly added condition rows have a stable React key (index
// keys would swap input state around on remove).
let _cid = 0;

// A newly added condition, seeded with the design's default params.
export function defaultCondition(type) {
  const _id = `c${(_cid += 1)}`;
  switch (type) {
    case 'max_spread':
      return { _id, type, pct: 5.0, min_ticks: 1 };
    case 'min_quote_size':
      return { _id, type, size: 10 };
    case 'min_premium':
      return { _id, type, points: 0.5 };
    case 'max_underlying_move':
      return { _id, type, pct: 1.0, ref: 'day_open' };
    case 'min_rehedge_delta':
      return { _id, type, threshold: 0.05 };
    default:
      return { _id, type };
  }
}

// Serialize a condition to the PINNED wire shape (drops UI-only `_id`, coerces
// numbers). Whitelisted per type so nothing extraneous leaks to the backend.
export function serializeCondition(c) {
  switch (c.type) {
    case 'max_spread':
      return { type: 'max_spread', pct: Number(c.pct), min_ticks: Number(c.min_ticks) };
    case 'min_quote_size':
      return { type: 'min_quote_size', size: Number(c.size) };
    case 'min_premium':
      return { type: 'min_premium', points: Number(c.points) };
    case 'max_underlying_move':
      return { type: 'max_underlying_move', pct: Number(c.pct), ref: c.ref || 'day_open' };
    case 'min_rehedge_delta':
      return { type: 'min_rehedge_delta', threshold: Number(c.threshold) };
    default:
      return { type: c.type };
  }
}

export function conditionLabel(type) {
  return LABELS[type] || type;
}

export default function ConditionEditor({ condition, onChange, onRemove, idPrefix }) {
  const c = condition;
  const set = (patch) => onChange({ ...c, ...patch });

  return (
    <div
      className={styles.conditionRow}
      data-testid={`${idPrefix}-condition-${c.type}`}
      data-condition-type={c.type}
    >
      <span className={styles.conditionLabel}>{conditionLabel(c.type)}</span>

      <div className={styles.conditionParams}>
        {c.type === 'max_spread' && (
          <>
            <label className={styles.paramField}>
              <span>Max spread %</span>
              <input
                type="number"
                step="0.1"
                min="0"
                aria-label={`${idPrefix} max_spread pct`}
                value={c.pct ?? ''}
                onChange={(e) => set({ pct: e.target.value })}
              />
            </label>
            <label className={styles.paramField}>
              <span>Min ticks</span>
              <input
                type="number"
                step="1"
                min="0"
                aria-label={`${idPrefix} max_spread min ticks`}
                value={c.min_ticks ?? ''}
                onChange={(e) => set({ min_ticks: e.target.value })}
              />
            </label>
          </>
        )}

        {c.type === 'min_quote_size' && (
          <label className={styles.paramField}>
            <span>Min quote size</span>
            <input
              type="number"
              step="1"
              min="0"
              aria-label={`${idPrefix} min_quote_size size`}
              value={c.size ?? ''}
              onChange={(e) => set({ size: e.target.value })}
            />
          </label>
        )}

        {c.type === 'min_premium' && (
          <label className={styles.paramField}>
            <span>Min premium (pts)</span>
            <input
              type="number"
              step="0.05"
              min="0"
              aria-label={`${idPrefix} min_premium points`}
              value={c.points ?? ''}
              onChange={(e) => set({ points: e.target.value })}
            />
          </label>
        )}

        {c.type === 'min_rehedge_delta' && (
          <label className={styles.paramField}>
            <span>Min rehedge delta</span>
            <input
              type="number"
              step="0.01"
              min="0"
              aria-label={`${idPrefix} min_rehedge_delta threshold`}
              value={c.threshold ?? ''}
              onChange={(e) => set({ threshold: e.target.value })}
            />
          </label>
        )}

        {c.type === 'max_underlying_move' && (
          <>
            <label className={styles.paramField}>
              <span>Max move %</span>
              <input
                type="number"
                step="0.1"
                min="0"
                aria-label={`${idPrefix} max_underlying_move pct`}
                value={c.pct ?? ''}
                onChange={(e) => set({ pct: e.target.value })}
              />
            </label>
            <label className={styles.paramField}>
              <span>Reference</span>
              <select
                aria-label={`${idPrefix} max_underlying_move ref`}
                value={c.ref || 'day_open'}
                onChange={(e) => set({ ref: e.target.value })}
              >
                <option value="day_open">Day open</option>
              </select>
            </label>
          </>
        )}
      </div>

      <button
        type="button"
        className={styles.chipRemove}
        aria-label={`Remove ${conditionLabel(c.type)} condition`}
        onClick={onRemove}
      >
        ×
      </button>
    </div>
  );
}
