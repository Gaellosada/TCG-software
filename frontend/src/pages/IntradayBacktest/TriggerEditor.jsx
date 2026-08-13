import styles from './IntradayBacktestPage.module.css';

// ---------------------------------------------------------------------------
// TriggerEditor — one row of the EXIT module's early-exit Triggers builder
// (DESIGN.md v3). Mirrors ConditionEditor: renders the param inputs for a
// single trigger (its `type` decides which) plus a remove ×. Triggers are
// EXIT-ONLY (entry has none) and close the straddle EARLY, before exit.time;
// the FIRST trigger to fire wins (OR). The 4 types are PINNED and hardcoded
// here — the frontend does NOT fetch them.
//
//  - underlying_move { amount, unit: points|percent }   (of ES_entry)
//  - sigma_move      { n }                                (n × implied σ-to-expiry)
//  - net_delta       { threshold }                        (|pre-hedge net delta|)
//  - pnl             { amount, unit: points|percent|usd, direction: profit|loss|both }
// ---------------------------------------------------------------------------

// The dropdown catalogue: order + label shown in the "Add trigger" select.
export const TRIGGER_TYPES = [
  { type: 'underlying_move', label: 'Underlying move' },
  { type: 'sigma_move', label: 'Sigma move' },
  { type: 'net_delta', label: 'Net delta' },
  { type: 'pnl', label: 'P&L' },
];

const LABELS = TRIGGER_TYPES.reduce((acc, t) => {
  acc[t.type] = t.label;
  return acc;
}, {});

// Monotonic id so freshly added trigger rows have a stable React key (index
// keys would swap input state around on remove).
let _tid = 0;

// A newly added trigger, seeded with the design's default params.
export function defaultTrigger(type) {
  const _id = `t${(_tid += 1)}`;
  switch (type) {
    case 'underlying_move':
      return { _id, type, amount: 15, unit: 'points' };
    case 'sigma_move':
      return { _id, type, n: 1.0 };
    case 'net_delta':
      return { _id, type, threshold: 0.30 };
    case 'pnl':
      return { _id, type, amount: 500, unit: 'usd', direction: 'both' };
    default:
      return { _id, type };
  }
}

// Serialize a trigger to the PINNED wire shape (drops UI-only `_id`, coerces
// numbers). Whitelisted per type so nothing extraneous leaks to the backend.
export function serializeTrigger(t) {
  switch (t.type) {
    case 'underlying_move':
      return { type: 'underlying_move', amount: Number(t.amount), unit: t.unit || 'points' };
    case 'sigma_move':
      return { type: 'sigma_move', n: Number(t.n) };
    case 'net_delta':
      return { type: 'net_delta', threshold: Number(t.threshold) };
    case 'pnl':
      return {
        type: 'pnl',
        amount: Number(t.amount),
        unit: t.unit || 'usd',
        direction: t.direction || 'both',
      };
    default:
      return { type: t.type };
  }
}

export function triggerLabel(type) {
  return LABELS[type] || type;
}

export default function TriggerEditor({ trigger, onChange, onRemove, idPrefix }) {
  const t = trigger;
  const set = (patch) => onChange({ ...t, ...patch });

  return (
    <div
      className={styles.conditionRow}
      data-testid={`${idPrefix}-trigger-${t.type}`}
      data-trigger-type={t.type}
    >
      <span className={styles.conditionLabel}>{triggerLabel(t.type)}</span>

      <div className={styles.conditionParams}>
        {t.type === 'underlying_move' && (
          <>
            <label className={styles.paramField}>
              <span>Amount</span>
              <input
                type="number"
                step="0.1"
                min="0"
                aria-label={`${idPrefix} underlying_move amount`}
                value={t.amount ?? ''}
                onChange={(e) => set({ amount: e.target.value })}
              />
            </label>
            <label className={styles.paramField}>
              <span>Unit</span>
              <select
                aria-label={`${idPrefix} underlying_move unit`}
                value={t.unit || 'points'}
                onChange={(e) => set({ unit: e.target.value })}
              >
                <option value="points">Points</option>
                <option value="percent">Percent</option>
              </select>
            </label>
          </>
        )}

        {t.type === 'sigma_move' && (
          <label className={styles.paramField}>
            <span>n × σ</span>
            <input
              type="number"
              step="0.1"
              min="0"
              aria-label={`${idPrefix} sigma_move n`}
              value={t.n ?? ''}
              onChange={(e) => set({ n: e.target.value })}
            />
          </label>
        )}

        {t.type === 'net_delta' && (
          <label className={styles.paramField}>
            <span>Threshold</span>
            <input
              type="number"
              step="0.01"
              min="0"
              aria-label={`${idPrefix} net_delta threshold`}
              value={t.threshold ?? ''}
              onChange={(e) => set({ threshold: e.target.value })}
            />
          </label>
        )}

        {t.type === 'pnl' && (
          <>
            <label className={styles.paramField}>
              <span>Amount</span>
              <input
                type="number"
                step="1"
                min="0"
                aria-label={`${idPrefix} pnl amount`}
                value={t.amount ?? ''}
                onChange={(e) => set({ amount: e.target.value })}
              />
            </label>
            <label className={styles.paramField}>
              <span>Unit</span>
              <select
                aria-label={`${idPrefix} pnl unit`}
                value={t.unit || 'usd'}
                onChange={(e) => set({ unit: e.target.value })}
              >
                <option value="points">Points</option>
                <option value="percent">Percent</option>
                <option value="usd">USD</option>
              </select>
            </label>
            <label className={styles.paramField}>
              <span>Direction</span>
              <select
                aria-label={`${idPrefix} pnl direction`}
                value={t.direction || 'both'}
                onChange={(e) => set({ direction: e.target.value })}
              >
                <option value="profit">Profit</option>
                <option value="loss">Loss</option>
                <option value="both">Both</option>
              </select>
            </label>
          </>
        )}
      </div>

      <button
        type="button"
        className={styles.chipRemove}
        aria-label={`Remove ${triggerLabel(t.type)} trigger`}
        onClick={onRemove}
      >
        ×
      </button>
    </div>
  );
}
